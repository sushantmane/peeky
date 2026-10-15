import json
import os
import sys

import pytest

from peeky.native import MAX_METADATA
from peeky.rust_symbols import (
    QUALIFIED_OPTIONS,
    UNQUALIFIED,
    UNVERIFIED,
    RustUnit,
    active_variant,
    application_symbols,
    application_units,
    enum_layouts,
    parse_dies,
)

APP = "src/main.rs/@/peeky_pagination.52664bd69b5859a2-cgu.0"
STD = "/rustc/abc/library/std/src/lib.rs/@/std.7d7552da0923e8b2-cgu.0"
PRODUCER = "clang LLVM (rustc version 1.96.0 (ac68faa20 2026-05-25))"
OPTION = "core::option::Option<i32>"


def entry(offset, depth, tag, *attributes):
    lines = [f"0x{offset:08x}:{' ' * (1 + 2 * depth)}{tag}"]
    lines += [f"{' ' * (14 + 2 * depth)}{name}\t({value})" for name, value in attributes]
    return lines


def null(offset, depth):
    return [f"0x{offset:08x}:{' ' * (1 + 2 * depth)}NULL"]


def unit(offset, end, *, name=APP, comp_dir=None, stmt=0, version=4, kind="Compile", **extra):
    attributes = []
    if extra.get("producer", PRODUCER) is not None:
        attributes.append(("DW_AT_producer", json.dumps(extra.get("producer", PRODUCER))))
    attributes.append(("DW_AT_language", extra.get("language", "DW_LANG_Rust")))
    if name is not None:
        attributes.append(("DW_AT_name", json.dumps(name)))
    attributes.append(("DW_AT_stmt_list", f"0x{stmt:08x}"))
    if comp_dir is not None:
        attributes.append(("DW_AT_comp_dir", json.dumps(str(comp_dir))))
    header = (
        f"0x{offset:08x}: {kind} Unit: length = 0x00000010, format = DWARF32, "
        f"version = 0x{version:04x}, abbr_offset = 0x0000, addr_size = 0x08 "
        f"(next unit at 0x{end:08x})"
    )
    return [header, "", *entry(offset + 11, 0, "DW_TAG_compile_unit", *attributes)]


def index(*units):
    lines = ["program:\tfile format Mach-O arm64", "", ".debug_info contents:"]
    for item in units:
        lines += item
    return "\n".join(lines) + "\n"


def standard_index(workspace, **app):
    return index(
        unit(0, 0x1000, comp_dir=workspace, **app),
        unit(0x1000, 0x2000, name=STD, comp_dir="/rustc/abc", stmt=0x228E),
    )


def option_dump(
    *,
    name="Option<i32>",
    discriminants=("0x00", "0x01"),
    variants=("None", "Some"),
    payload="i32",
    tag_type="u32",
    artificial=True,
    scope="DW_TAG_namespace",
    root=0x0B,
    extra_offset=None,
    i32=("DW_TAG_base_type", "DW_ATE_signed", "0x04"),
    u32=("DW_TAG_base_type", "DW_ATE_unsigned", "0x04"),
    closing=True,
):
    tag = [("DW_AT_type", f'0x00000400 "{tag_type}"'), ("DW_AT_data_member_location", "0x00")]
    if artificial:
        tag.append(("DW_AT_artificial", "true"))
    lines = [
        *entry(root, 0, "DW_TAG_compile_unit", ("DW_AT_name", json.dumps(APP))),
        *entry(0x100, 1, "DW_TAG_namespace", ("DW_AT_name", '"core"')),
        *entry(0x110, 2, scope, ("DW_AT_name", '"option"')),
        *entry(0x120, 3, "DW_TAG_structure_type", ("DW_AT_name", json.dumps(name))),
        *entry(0x130, 4, "DW_TAG_variant_part", ("DW_AT_discr", "0x00000140")),
        *entry(0x140, 5, "DW_TAG_member", *tag),
    ]
    targets = {"None": 0x200, "Some": 0x210}
    for index_, (value, variant) in enumerate(zip(discriminants, variants)):
        base = 0x150 + index_ * 0x30
        attributes = [("DW_AT_discr_value", value)] if value is not None else []
        lines += entry(base, 5, "DW_TAG_variant", *attributes)
        lines += entry(
            base + 0x10,
            6,
            "DW_TAG_member",
            ("DW_AT_name", json.dumps(variant)),
            ("DW_AT_type", f'0x{targets.get(variant, 0x200):08x} "{variant}"'),
        )
        lines += null(base + 0x20, 6)
    lines += null(0x1B0, 5)
    lines += entry(0x200, 4, "DW_TAG_structure_type", ("DW_AT_name", '"None"'))
    lines += entry(0x204, 5, "DW_TAG_template_type_parameter", ("DW_AT_type", '0x00000300 "i32"'))
    lines += null(0x208, 5)
    lines += entry(0x210, 4, "DW_TAG_structure_type", ("DW_AT_name", '"Some"'))
    lines += entry(
        0x218,
        5,
        "DW_TAG_member",
        ("DW_AT_name", '"__0"'),
        ("DW_AT_type", f'0x00000300 "{payload}"'),
        ("DW_AT_data_member_location", "0x04"),
    )
    lines += null(0x21C, 5)
    lines += null(0x220, 4)
    lines += null(0x230, 3)
    lines += null(0x240, 2)
    for offset, name, base in ((0x300, "i32", i32), (extra_offset or 0x400, "u32", u32)):
        if base is not None:
            tag, encoding, size = base
            lines += entry(
                offset,
                1,
                tag,
                ("DW_AT_name", json.dumps(name)),
                ("DW_AT_encoding", encoding),
                ("DW_AT_byte_size", size),
            )
    if closing:
        lines += null(0x500, 1)
    return "\n".join(lines) + "\n"


UNIT = RustUnit(0, 0x0B, 0x1000, 0, APP)


def test_application_units_select_only_local_crate_units(tmp_path):
    units = application_units(
        standard_index(tmp_path), tmp_path, entrypoint="src/main.rs", crate="peeky_pagination"
    )
    assert units == (RustUnit(0, 0x0B, 0x1000, 0, APP),)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "src/other.rs/@/peeky_pagination.52664bd69b5859a2-cgu.0"},
        {"name": "src/main.rs/@/other_crate.52664bd69b5859a2-cgu.0"},
        {"name": None},
        {"comp_dir": "/elsewhere"},
        {"comp_dir": "relative"},
        {"comp_dir": None},
        {"version": 5},
        {"kind": "Partial"},
        {"language": "DW_LANG_C11"},
        {"producer": None},
        {"producer": "clang version 18"},
    ],
)
def test_application_units_reject_ambiguous_or_unsupported_units(tmp_path, change):
    workspace = change.pop("comp_dir", tmp_path)
    text = index(
        unit(0, 0x1000, comp_dir=workspace, **change),
        unit(0x1000, 0x2000, name=STD, comp_dir="/rustc/abc", stmt=0x228E),
    )
    with pytest.raises(ValueError):
        application_units(text, tmp_path, entrypoint="src/main.rs", crate="peeky_pagination")


@pytest.mark.parametrize("alias", ["{w}/", "{w}/.", "{w}/../{n}", "link"])
def test_application_units_require_the_exact_recorded_workspace(tmp_path, alias):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    (tmp_path / "link").symlink_to(workspace)
    recorded = (
        str(tmp_path / "link") if alias == "link" else alias.format(w=workspace, n=workspace.name)
    )
    with pytest.raises(ValueError, match="Ambiguous"):
        application_units(
            index(unit(0, 0x1000, comp_dir=recorded)),
            workspace,
            entrypoint="src/main.rs",
            crate="peeky_pagination",
        )


def test_application_units_reject_missing_duplicate_or_excess_units(tmp_path):
    std_only = index(unit(0, 0x1000, name=STD, comp_dir="/rustc/abc"))
    duplicate = index(unit(0, 0x1000, comp_dir=tmp_path), unit(0x1000, 0x2000, comp_dir=tmp_path))
    excess = index(
        *(
            unit(index_ * 0x100, (index_ + 1) * 0x100, comp_dir=tmp_path, stmt=index_ * 0x10)
            for index_ in range(17)
        )
    )
    for text in (std_only, duplicate, excess):
        with pytest.raises(ValueError):
            application_units(text, tmp_path, entrypoint="src/main.rs", crate="peeky_pagination")
    with pytest.raises(ValueError, match="crate"):
        application_units(
            standard_index(tmp_path), tmp_path, entrypoint="src/main.rs", crate="Bad-Crate"
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text + "unexpected row\n",
        lambda text: text.replace("              DW_AT_language", "                DW_AT_language"),
        lambda text: text + "0x00003000:     NULL\n",
        lambda text: text.replace("DW_AT_stmt_list", "DW_AT_language"),
        lambda text: text.replace('"/rustc/abc"', '"/rustc/\\303abc"'),
        lambda text: text.replace(".debug_info contents:", "")
        + "program:\tfile format Mach-O arm64\n",
        lambda text: text.replace("0x0000100b: DW_TAG_compile_unit", ""),
    ],
)
def test_die_parser_fails_closed_on_unrecognized_or_unbalanced_output(tmp_path, mutate):
    with pytest.raises(ValueError):
        application_units(
            mutate(standard_index(tmp_path)),
            tmp_path,
            entrypoint="src/main.rs",
            crate="peeky_pagination",
        )


def test_die_parser_tolerates_only_attribute_range_continuations():
    text = "\n".join(
        [
            *entry(0x0B, 0, "DW_TAG_compile_unit"),
            "              DW_AT_ranges\t(0x00000000",
            "                [0x0000000000001000, 0x0000000000001010)",
            "                [0x0000000000002000, 0x0000000000002010): DW_OP_reg0 W0)",
        ]
    )
    (root,) = parse_dies(text, units=False)
    assert root.attributes == {"DW_AT_ranges": "0x00000000"}
    with pytest.raises(ValueError):
        parse_dies(
            "\n".join(
                [
                    *entry(0x0B, 0, "DW_TAG_compile_unit"),
                    "  [0x0000000000001000, 0x0000000000001010)",
                ]
            ),
            units=False,
        )
    with pytest.raises(ValueError, match="exceeds"):
        parse_dies(" " * (MAX_METADATA + 1), units=False)


def test_enum_layouts_follow_compiler_discriminants_and_qualify_only_i32():
    layouts = enum_layouts(option_dump(), UNIT)
    assert layouts == {
        OPTION: {
            "kind": "option",
            "discriminant": "u32",
            "variants": [[0, "None"], [1, "Some"]],
            "payload": "i32",
        }
    }
    assert enum_layouts(option_dump(discriminants=("0x07", "0x02")), UNIT)[OPTION]["variants"] == [
        [7, "None"],
        [2, "Some"],
    ]
    assert set(QUALIFIED_OPTIONS) == {OPTION}


@pytest.mark.parametrize(
    "options",
    [
        {"name": "Option<u32>", "payload": "u32"},
        {"payload": "u32"},
        {"discriminants": ("0x00", None)},
        {"discriminants": ("0x01", "0x01")},
        {"variants": ("Some", "None")},
        {"discriminants": ("0x00",), "variants": ("None",)},
        {"tag_type": "u8"},
        {"artificial": False},
        {"scope": "DW_TAG_subprogram"},
        {"i32": ("DW_TAG_pointer_type", "DW_ATE_signed", "0x04")},
        {"i32": ("DW_TAG_base_type", "DW_ATE_unsigned", "0x04")},
        {"i32": ("DW_TAG_base_type", "DW_ATE_signed", "0x08")},
        {"u32": ("DW_TAG_base_type", "DW_ATE_signed", "0x04")},
        {"i32": None},
        {"u32": None},
        {"discriminants": ("0x00", "0x100000000")},
    ],
)
def test_enum_layouts_mark_niche_unexpected_or_unscoped_layouts_unqualified(options):
    layouts = enum_layouts(option_dump(**options), UNIT)
    assert len(layouts) == 1
    assert next(iter(layouts.values())) == {"kind": "unqualified"}


def test_die_tree_requires_every_child_list_to_close():
    text = option_dump()
    with pytest.raises(ValueError, match="Unterminated"):
        enum_layouts(option_dump(closing=False), UNIT)
    marker = "DW_AT_data_member_location\t(0x04)"
    truncated = text[: text.index(marker) + len(marker) + 1]
    with pytest.raises(ValueError, match="Unterminated"):
        enum_layouts(truncated, UNIT)
    missing = text.replace("0x0000021c:           NULL\n", "")
    with pytest.raises(ValueError, match="Unterminated"):
        enum_layouts(missing, UNIT)
    extra = text.replace(
        "0x00000230:       NULL\n", "0x00000230:       NULL\n0x00000231:       NULL\n"
    )
    with pytest.raises(ValueError, match="Unbalanced"):
        enum_layouts(extra, UNIT)


def test_die_attributes_indent_by_depth_not_offset_width():
    wide = "\n".join(
        [
            "0x000000010000000b: DW_TAG_compile_unit",
            '              DW_AT_name\t("x")',
            "0x0000000100000010:   DW_TAG_base_type",
            '                DW_AT_name\t("i32")',
            "0x0000000100000020:   NULL",
        ]
    )
    (root,) = parse_dies(wide, units=False)
    assert root.offset == 0x10000000B and root.children[0].attributes["DW_AT_name"] == '"i32")'
    with pytest.raises(ValueError, match="attached"):
        parse_dies(
            wide.replace(
                '                DW_AT_name\t("i32")', '                        DW_AT_name\t("i32")'
            ),
            units=False,
        )


def test_enum_layouts_bind_entries_to_the_selected_unit():
    with pytest.raises(ValueError, match="does not match"):
        enum_layouts(option_dump(root=0x0C), UNIT)
    with pytest.raises(ValueError, match="outside"):
        enum_layouts(option_dump(extra_offset=0x1000), UNIT)
    with pytest.raises(ValueError, match="duplicated"):
        enum_layouts(option_dump(extra_offset=0x300), UNIT)


def fake_dwarfdump(tmp_path, files):
    data = tmp_path / "fake-dwarf"
    data.mkdir()
    for name, text in files.items():
        (data / name).write_text(text)
    script = tmp_path / "dwarfdump"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "name = 'index' if '--recurse-depth=0' in args else args[0].split('=', 1)[1]\n"
        "if '--debug-line=' in args[0]: name = 'line-' + name\n"
        "source = pathlib.Path(os.environ['FAKE_DWARF']) / name\n"
        "pathlib.Path(args[args.index('-o') + 1]).write_text(source.read_text())\n"
    )
    script.chmod(0o755)
    return str(script), {**os.environ, "FAKE_DWARF": str(data)}


def line_table(offset=0, line=2):
    return f"""program:\tfile format ELF64-x86-64
.debug_line contents:
debug_line[0x{offset:08x}]
version: 4
include_directories[  1] = "/rustc/abc"
include_directories[  2] = "src"
file_names[  1]:
name: "library/core/src/option.rs"
dir_index: 1
file_names[  2]:
name: "main.rs"
dir_index: 2
Address Line Column File
0x0000000000401000 900 1 1 0 0 is_stmt
0x0000000000401008 {line} 1 2 0 0 is_stmt
0x0000000000401010 {line} 1 2 0 0 end_sequence
"""


def rust_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/main.rs").write_text("fn main() {\n    let x = 1;\n}\n")
    return workspace


def test_application_symbols_share_one_deadline(tmp_path, monkeypatch):
    workspace = rust_workspace(tmp_path)
    tool, env = fake_dwarfdump(
        tmp_path,
        {
            "index": standard_index(workspace),
            "line-0x00000000": line_table(),
            "0x0000000b": option_dump(),
        },
    )
    clock = iter([0.0, 1.0, 31.0])
    monkeypatch.setattr("peeky.rust_symbols._clock", lambda: next(clock))
    with pytest.raises(ValueError, match="deadline"):
        application_symbols(
            tool,
            "program",
            workspace,
            env,
            entrypoint="src/main.rs",
            crate="peeky_pagination",
            declared=("src/main.rs",),
        )
    with pytest.raises(ValueError, match="time limit"):
        from peeky.native import _metadata

        _metadata(tool, [], workspace, env, timeout=16)


def test_application_symbols_dump_only_selected_tables(tmp_path):
    workspace = rust_workspace(tmp_path)
    tool, env = fake_dwarfdump(
        tmp_path,
        {
            "index": standard_index(workspace),
            "line-0x00000000": line_table(),
            "0x0000000b": option_dump(),
        },
    )
    lines, layouts, units = application_symbols(
        tool,
        "program",
        workspace,
        env,
        entrypoint="src/main.rs",
        crate="peeky_pagination",
        declared=("src/main.rs",),
    )
    assert lines == {"src/main.rs": [2]}
    assert layouts[OPTION]["kind"] == "option"
    assert [unit.stmt_list for unit in units] == [0]
    assert not list(workspace.glob("peeky-symbols-*"))


@pytest.mark.parametrize(
    "files",
    [
        {"line-0x00000000": line_table(offset=0x228E)},
        {"line-0x00000000": line_table() + line_table(offset=0x228E)},
        {"line-0x00000000": line_table(line=9)},
        {"0x0000000b": option_dump(root=0x0C)},
    ],
)
def test_application_symbols_reject_mismatched_tables(tmp_path, files):
    workspace = rust_workspace(tmp_path)
    tool, env = fake_dwarfdump(
        tmp_path,
        {
            "index": standard_index(workspace),
            "line-0x00000000": line_table(),
            "0x0000000b": option_dump(),
            **files,
        },
    )
    with pytest.raises(ValueError):
        application_symbols(
            tool,
            "program",
            workspace,
            env,
            entrypoint="src/main.rs",
            crate="peeky_pagination",
            declared=("src/main.rs",),
        )


LAYOUTS = {
    OPTION: {
        "kind": "option",
        "discriminant": "u32",
        "variants": [[0, "None"], [1, "Some"]],
        "payload": "i32",
    },
    "core::option::Option<&i32>": {"kind": "unqualified"},
}


def raw_option(discriminants=("1", "1"), payload=None, names=("$variant$0", "$variant$1")):
    inner = f"{OPTION}::{OPTION}$Inner"
    children = {
        7: [{"name": "$variants$", "type": inner, "variablesReference": 13}],
        13: [
            {"name": names[0], "type": inner + "::None$Variant", "variablesReference": 14},
            {"name": names[1], "type": inner + "::Some$Variant", "variablesReference": 15},
        ],
        14: [
            {"name": "$discr$", "type": "unsigned int", "value": discriminants[0]},
            {"name": "value", "type": f"{OPTION}::None<int>", "variablesReference": 17},
        ],
        15: [
            {"name": "$discr$", "type": "unsigned int", "value": discriminants[1]},
            {"name": "value", "type": f"{OPTION}::Some<int>", "variablesReference": 19},
        ],
        17: [{"name": "__0", "type": "int", "value": "1806299584", "variablesReference": 0}],
        19: [payload or {"name": "__0", "type": "int", "value": "3", "variablesReference": 0}],
    }
    return {"name": "some", "type": OPTION, "value": "...", "variablesReference": 7}, children


def fetcher(children):
    seen = []

    def fetch(reference):
        seen.append(reference)
        return [dict(value) for value in children[reference]]

    return fetch, seen


def test_active_variant_reads_only_the_compiled_active_payload():
    variable, children = raw_option()
    fetch, seen = fetcher(children)
    assert active_variant(variable, fetch, LAYOUTS) == {
        "name": "some",
        "type": OPTION,
        "variablesReference": 0,
        "value": "Some(3)",
        "rust_view": "active-variant",
    }
    assert 17 not in seen and len(seen) == 5
    variable, children = raw_option(("0", "0"))
    fetch, seen = fetcher(children)
    assert active_variant(variable, fetch, LAYOUTS)["value"] == "None"
    assert not {17, 19} & set(seen)


def test_active_variant_binds_debugger_variant_names_to_dwarf_discriminants():
    layouts = {OPTION: {**LAYOUTS[OPTION], "variants": [[5, "None"], [9, "Some"]]}}
    named = ("$variant$5", "$variant$9")
    variable, children = raw_option(("9", "9"), names=named)
    assert active_variant(variable, fetcher(children)[0], layouts)["value"] == "Some(3)"
    variable, children = raw_option(("5", "5"), names=named)
    assert active_variant(variable, fetcher(children)[0], layouts)["value"] == "None"
    for discriminants, names in ((("1", "1"), named), (("9", "9"), ("$variant$0", "$variant$1"))):
        variable, children = raw_option(discriminants, names=names)
        assert active_variant(variable, fetcher(children)[0], layouts)["value"] == UNVERIFIED


def test_non_enum_and_unqualified_enum_values_do_not_fetch_raw_storage():
    fetch, seen = fetcher({})
    assert active_variant({"name": "x", "type": "int", "value": "1"}, fetch, LAYOUTS) is None
    view = active_variant(
        {"name": "r", "type": "core::option::Option<&i32>", "variablesReference": 3},
        fetch,
        LAYOUTS,
    )
    assert view == {
        "name": "r",
        "type": "core::option::Option<&i32>",
        "variablesReference": 0,
        "value": UNQUALIFIED,
        "rust_view": "opaque",
    }
    assert seen == []


def corrupt(children, reference, index_, **changes):
    children[reference][index_] = {**children[reference][index_], **changes}
    return children


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: corrupt(c, 7, 0, name="$variant$"),
        lambda c: corrupt(c, 13, 1, name="$variant$"),
        lambda c: corrupt(c, 7, 0, type="other"),
        lambda c: {**c, 7: c[7] * 2},
        lambda c: corrupt(c, 13, 0, type="wrong$Variant"),
        lambda c: {**c, 13: list(reversed(c[13]))},
        lambda c: corrupt(c, 14, 0, value="0"),
        lambda c: corrupt(c, 14, 0, value="0x1"),
        lambda c: corrupt(c, 15, 0, type="int"),
        lambda c: corrupt(c, 15, 0, variablesReference=3),
        lambda c: {**c, 14: c[14][:1]},
        lambda c: corrupt(corrupt(c, 14, 0, value="2"), 15, 0, value="2"),
        lambda c: corrupt(c, 19, 0, type="unsigned int"),
        lambda c: corrupt(c, 19, 0, value="2147483648"),
        lambda c: corrupt(c, 19, 0, value="03"),
        lambda c: corrupt(c, 19, 0, variablesReference=4),
        lambda c: {**c, 19: []},
        lambda c: corrupt(c, 15, 1, variablesReference=0),
        lambda c: {**c, 14: [c[14][0], c[15][1]], 15: [c[15][0], c[14][1]]},
        lambda c: corrupt(c, 15, 1, type=f"{OPTION}::Some<long>"),
    ],
)
def test_active_variant_shape_mismatch_is_opaque_not_guessed(mutate):
    variable, children = raw_option()
    view = active_variant(variable, fetcher(mutate(children))[0], LAYOUTS)
    assert view["value"] == UNVERIFIED and view["rust_view"] == "opaque"
    assert view["variablesReference"] == 0
