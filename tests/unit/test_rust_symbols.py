import json
import os
import re
import sys

import pytest

from peeky.native import MAX_METADATA
from peeky.rust_symbols import (
    QUALIFIED_OPTIONS,
    RUST_METADATA,
    UNQUALIFIED,
    UNVERIFIED,
    RustUnit,
    active_variant,
    application_symbols,
    application_units,
    code_blocks,
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
        lambda text: (
            text.replace(".debug_info contents:", "") + "program:\tfile format Mach-O arm64\n"
        ),
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
        parse_dies(" " * (RUST_METADATA + 1), units=False)
    # A program using std's collections has megabytes of its own DWARF (eval 3 follow-up).
    with pytest.raises(ValueError, match="Missing DWARF unit"):  # past the size check
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
    lines, layouts, units, blocks = application_symbols(
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
    assert blocks == {}
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


def _tree_dump(tree, unit_offset=0x0B):
    """Serialize (label, tag, attributes, children) nodes in DFS order; an attribute
    value ("ref", label) becomes that node's offset and quoted name."""
    offsets, names, order = {}, {}, []

    def assign(node, depth, offset):
        label, tag, attributes, children = node
        offsets[label], names[label] = offset, dict(attributes).get("DW_AT_name")
        order.append((node, depth))
        offset += 8
        for child in children:
            offset = assign(child, depth + 1, offset)
        if children:
            order.append((("null", offset), depth + 1))
            offset += 1
        return offset

    end = assign(tree, 0, unit_offset)
    lines = []
    for node, depth in order:
        if node[0] == "null":
            lines += null(node[1], depth)
            continue
        label, tag, attributes, _ = node
        rendered = []
        for name, value in attributes:
            if isinstance(value, tuple):
                target = value[1]
                quoted = names.get(target)
                value = f"0x{offsets[target]:08x}" + (f" {quoted}" if quoted else "")
            rendered.append((name, value))
        lines += entry(offsets[label], depth, tag, *rendered)
    return "\n".join(lines) + "\n", RustUnit(0, unit_offset, end + 16, 0, APP)


def _base(label, name, size, encoding):
    return (
        label,
        "DW_TAG_base_type",
        [("DW_AT_name", f'"{name}"'), ("DW_AT_encoding", encoding), ("DW_AT_byte_size", size)],
        [],
    )


def _member(label, name, target, offset):
    return (
        label,
        "DW_TAG_member",
        [
            ("DW_AT_name", f'"{name}"'),
            ("DW_AT_type", ("ref", target)),
            ("DW_AT_data_member_location", offset),
        ],
        [],
    )


def _struct(label, name, size, *children):
    return (
        label,
        "DW_TAG_structure_type",
        [("DW_AT_name", f'"{name}"'), ("DW_AT_byte_size", size)],
        list(children),
    )


def _param(label, target):
    return (
        label,
        "DW_TAG_template_type_parameter",
        [("DW_AT_type", ("ref", target)), ("DW_AT_name", '"T"')],
        [],
    )


def _namespace(label, name, *children):
    return (label, "DW_TAG_namespace", [("DW_AT_name", f'"{name}"')], list(children))


def container_dump(*, len_offset="0x10", element="i32", data_ptr="i32_ptr"):
    vec = lambda label, t, raw: _struct(  # noqa: E731
        label,
        f"Vec<{t}, alloc::alloc::Global>",
        "0x18",
        _param(f"{label}_t", t),
        _member(f"{label}_buf", "buf", raw, "0x00"),
        _member(f"{label}_len", "len", "usize", len_offset),
    )
    raw_vec = lambda label, t: _struct(  # noqa: E731
        label,
        f"RawVec<{t}, alloc::alloc::Global>",
        "0x10",
        _param(f"{label}_t", t),
        _member(f"{label}_inner", "inner", "inner", "0x00"),
    )
    tree = (
        "cu",
        "DW_TAG_compile_unit",
        [("DW_AT_name", '"src/main.rs/@/median"')],
        [
            _namespace(
                "alloc",
                "alloc",
                _namespace(
                    "vec_ns", "vec", vec("vec", element, "raw_i32"), vec("vec_u8", "u8", "raw_u8")
                ),
                _namespace(
                    "raw_ns",
                    "raw_vec",
                    raw_vec("raw_i32", element),
                    raw_vec("raw_u8", "u8"),
                    _struct(
                        "inner",
                        "RawVecInner<alloc::alloc::Global>",
                        "0x10",
                        _member("inner_ptr", "ptr", "unique", "0x08"),
                        _member("inner_cap", "cap", "cap", "0x00"),
                    ),
                ),
                _namespace(
                    "string_ns",
                    "string",
                    _struct("string", "String", "0x18", _member("s_vec", "vec", "vec_u8", "0x00")),
                ),
            ),
            _namespace(
                "core",
                "core",
                _struct(
                    "unique", "Unique<u8>", "0x08", _member("u_ptr", "pointer", "nonnull", "0x00")
                ),
                _struct(
                    "nonnull", "NonNull<u8>", "0x08", _member("n_ptr", "pointer", "u8_ptr", "0x00")
                ),
                _struct("cap", "UsizeNoHighBit", "0x08", _member("cap0", "__0", "usize", "0x00")),
            ),
            _struct(
                "slice",
                "&[i32]",
                "0x10",
                _member("sl_data", "data_ptr", data_ptr, "0x00"),
                _member("sl_len", "length", "usize", "0x08"),
            ),
            _struct(
                "str",
                "&str",
                "0x10",
                _member("st_data", "data_ptr", "u8_ptr", "0x00"),
                _member("st_len", "length", "usize", "0x08"),
            ),
            _base("i32", "i32", "0x04", "DW_ATE_signed"),
            _base("u8", "u8", "0x01", "DW_ATE_unsigned"),
            _base("usize", "usize", "0x08", "DW_ATE_unsigned"),
            ("u8_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "u8"))], []),
            ("i32_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "i32"))], []),
        ],
    )
    return _tree_dump(tree)


def test_container_layouts_follow_the_compiler_member_chain():
    from peeky.rust_symbols import container_layouts

    text, unit_ = container_dump()
    layouts = container_layouts(text, unit_)
    assert layouts["alloc::vec::Vec<i32, alloc::alloc::Global>"] == {
        "kind": "vec",
        "size": 0x18,
        "data": 8,
        "length": 0x10,
        "capacity": 0,
        "element": "i32",
        "element_size": 4,
    }
    assert layouts["alloc::string::String"]["kind"] == "string"
    assert layouts["&[i32]"] == {
        "kind": "slice",
        "size": 0x10,
        "data": 0,
        "length": 8,
        "element": "i32",
        "element_size": 4,
    }
    assert layouts["&str"]["element"] == "u8"


@pytest.mark.parametrize(
    "change",
    [
        {"len_offset": "0x08"},  # overlaps the data pointer
        {"element": "u8"},  # Vec<u8> is not the qualified Vec<i32>
        {"data_ptr": "u8_ptr"},  # &[i32] must point at i32
    ],
)
def test_container_layouts_refuse_anything_unexpected(change):
    from peeky.rust_symbols import container_layouts

    text, unit_ = container_dump(**change)
    layouts = container_layouts(text, unit_)
    if change.get("element"):
        assert "alloc::vec::Vec<i32, alloc::alloc::Global>" not in layouts
    else:
        assert any(layout == {"kind": "unqualified"} for layout in layouts.values())


VEC = "alloc::vec::Vec<int, alloc::alloc::Global>"
VEC_NAME = "alloc::vec::Vec<i32, alloc::alloc::Global>"
VEC_LAYOUT = {
    "kind": "vec",
    "size": 24,
    "data": 8,
    "length": 16,
    "capacity": 0,
    "element": "i32",
    "element_size": 4,
}
SLICE_LAYOUT = {
    "kind": "slice",
    "size": 16,
    "data": 0,
    "length": 8,
    "element": "i32",
    "element_size": 4,
}
STR_LAYOUT = {"kind": "str", "size": 16, "data": 0, "length": 8, "element": "u8", "element_size": 1}
CONTAINERS = {VEC_NAME: VEC_LAYOUT, "&[i32]": SLICE_LAYOUT, "&str": STR_LAYOUT}


def memory(blocks):
    def read(address, count):
        block = blocks[address]
        assert len(block) >= count
        return block[:count]

    return read


def words(*values):
    return b"".join(value.to_bytes(8, "little") for value in values)


def ints(*values):
    return b"".join(value.to_bytes(4, "little", signed=True) for value in values)


def view(variable, read, parameter, optimized=False):
    from peeky.rust_symbols import container_view

    return container_view(variable, read, CONTAINERS, optimized=optimized, parameter=parameter)


def test_only_proven_reference_parameters_are_read_from_memory():
    read = memory({"0x10": words(0x1000, 3), "0x1000": ints(5, -1, 3)})
    slice_ = {"name": "values", "type": "&[i32]", "memoryReference": "0x10"}
    shown = view(slice_, read, "&[i32]")
    assert shown["value"] == "[5, -1, 3]" and shown["length"] == 3
    names = [child["name"] for child in shown["children"]]
    assert names == ["length", "[0]", "[1]", "[2]"]
    assert shown["children"][0] == {"name": "length", "type": "usize", "value": "3"}
    # Locals (no proof), and a proof for another type, leave the value raw: a moved or
    # dropped local can still point at freed memory.
    assert view(slice_, read, None) is None
    assert view(slice_, read, "&str") is None
    assert view({**slice_, "type": VEC}, read, "&[i32]") is None
    text = "größe".encode()
    read = memory({"0x20": words(0x2000, len(text)), "0x2000": text})
    label = {"name": "label", "type": "&str", "memoryReference": "0x20"}
    shown = view(label, read, "&str")
    assert shown["value"] == '"größe"'
    assert shown["children"] == [{"name": "length", "type": "usize", "value": str(len(text))}]


@pytest.mark.parametrize("declared", [f"&{VEC_NAME}", f"&mut {VEC_NAME}"])
def test_vec_references_are_read_through_their_pointer(declared):
    read = memory({"0x10": words(0x2000), "0x2000": words(4, 0x3000, 2), "0x3000": ints(7, 8)})
    variable = {"name": "list", "type": f"{VEC} *", "memoryReference": "0x10"}
    shown = view({**variable, "value": "0x0000000000002000"}, read, declared)
    assert shown["value"] == "[7, 8]"
    assert [child["name"] for child in shown["children"]] == ["len", "capacity", "[0]", "[1]"]
    assert shown["children"][:2] == [
        {"name": "len", "type": "usize", "value": "2"},
        {"name": "capacity", "type": "usize", "value": "4"},
    ]


@pytest.mark.parametrize(
    "slot, value, reason",
    [
        (0x2000, "0x0000000000002008", "the slot disagrees with the debugger's pointer"),
        (0x2000, "<not a pointer>", "no pointer to cross-check"),
        (0, "0x0", "null reference"),
        (0x2004, "0x2004", "misaligned reference"),
    ],
)
def test_vec_references_that_do_not_check_out_are_never_read(slot, value, reason):
    from peeky.rust_symbols import UNVERIFIED_CONTAINER

    read = memory(
        {
            "0x10": words(slot),
            "0x2000": words(4, 0x3000, 2),
            "0x2004": words(4, 0x3000, 2),
            "0x3000": ints(7, 8),
        }
    )
    variable = {"name": "v", "type": f"{VEC} *", "memoryReference": "0x10", "value": value}
    assert view(variable, read, f"&{VEC_NAME}")["value"] == UNVERIFIED_CONTAINER, reason


def test_a_slice_away_from_the_debuggers_address_is_never_read():
    from peeky.rust_symbols import UNVERIFIED_CONTAINER

    read = memory({"0x10": words(0x1000, 2), "0x1000": ints(1, 2)})
    variable = {"name": "s", "type": "&[i32]", "memoryReference": "0x10"}
    assert view({**variable, "value": "&[i32] @ 0x10"}, read, "&[i32]")["value"] == "[1, 2]"
    shown = view({**variable, "value": "&[i32] @ 0x18"}, read, "&[i32]")
    assert shown["value"] == UNVERIFIED_CONTAINER


@pytest.mark.parametrize(
    "header, reason",
    [
        (words(2, 0x1000, 3), "length above capacity"),
        (words(4, 0, 3), "null buffer"),
        (words(4, 0x1001, 3), "misaligned buffer"),
    ],
)
def test_garbage_containers_are_never_shown_as_contents(header, reason):
    from peeky.rust_symbols import UNVERIFIED_CONTAINER

    read = memory({"0x10": words(0x20), "0x20": header, "0x1000": bytes(16), "0x1001": bytes(16)})
    variable = {"name": "v", "type": f"{VEC} *", "memoryReference": "0x10", "value": "0x20"}
    assert view(variable, read, f"&{VEC_NAME}")["value"] == UNVERIFIED_CONTAINER, reason


def test_a_slice_longer_than_isize_max_bytes_is_never_read():
    from peeky.rust_symbols import UNVERIFIED_CONTAINER

    read = memory({"0x10": words(0x1000, 2**61), "0x1000": bytes(64)})
    variable = {"name": "s", "type": "&[i32]", "memoryReference": "0x10"}
    assert view(variable, read, "&[i32]")["value"] == UNVERIFIED_CONTAINER


def test_long_containers_keep_whole_elements_and_their_marker_within_the_value_limit():
    from peeky.rust_symbols import MAX_ELEMENTS, MAX_SHOWN

    count = 300
    numbers = [1_000_000 + i for i in range(count)]
    read = memory({"0x10": words(0x1000, count), "0x1000": ints(*numbers)})
    shown = view({"name": "big", "type": "&[i32]", "memoryReference": "0x10"}, read, "&[i32]")
    value = shown["value"]
    assert len(value) <= MAX_SHOWN and value.endswith(f", ...] (first {value.count(',')} of 300)")
    listed = value[1 : value.index(", ...]")].split(", ")
    assert listed == [str(n) for n in numbers[: len(listed)]]
    # Selectors still reach every decoded element.
    assert len(shown["children"]) == 1 + MAX_ELEMENTS and shown["length"] == count
    text = ("ä" * 1500 + "tail").encode()
    read = memory({"0x20": words(0x2000, len(text)), "0x2000": text})
    shown = view({"name": "t", "type": "&str", "memoryReference": "0x20"}, read, "&str")
    value = shown["value"]
    marker = re.search(r'" \(first ([0-9]+) of ([0-9]+) bytes\)$', value)
    assert len(value) <= MAX_SHOWN and marker and marker[2] == str(len(text))
    assert json.loads(value[: marker.start() + 1]).encode() == text[: int(marker[1])]


def test_container_views_are_opaque_when_optimized_and_raw_without_memory():
    from peeky.rust_symbols import UNVERIFIED_CONTAINER

    read = memory({"0x10": words(0x1000, 1), "0x1000": ints(1)})
    variable = {"name": "s", "type": "&[i32]", "memoryReference": "0x10"}
    assert view(variable, read, "&[i32]", optimized=True)["value"] == UNVERIFIED_CONTAINER
    # No memory reads (LLVM 18 and 19 lldb-dap) or no reference: the raw value stays.
    assert view(variable, None, "&[i32]") is None
    assert view({**variable, "memoryReference": None}, read, "&[i32]") is None
    assert view({"name": "x", "type": "i32"}, read, "&[i32]") is None


def test_rust_sessions_read_containers_only_past_a_proven_parameters_declaration():
    from types import SimpleNamespace

    from peeky.rust import RustSession

    read = memory({"0x10": words(0x1000, 1), "0x1000": ints(9)})
    variable = {"name": "values", "type": "&[i32]", "memoryReference": "0x10"}
    session = SimpleNamespace(
        parameters={("h0123456789abcdef", "values"): {"type": "&[i32]", "line": 4}},
        layouts=CONTAINERS,
        target=SimpleNamespace(opt_level="0"),
    )
    frame = {"name": "demo::median::h0123456789abcdef", "line": 5}

    def shown(**changes):
        arguments = {"frame": frame, "top": True, **changes}
        result = RustSession.view_variable(session, variable, None, read, **arguments)
        return (result or {}).get("value")

    assert shown() == "[9]"
    # On the declaration lines the entry block may not have stored the parameter yet.
    assert shown(frame={**frame, "line": 4}) is None
    assert shown(frame={**frame, "line": None}) is None
    # Another function (hash), a field of something (not top-level), or no frame.
    assert shown(frame={**frame, "name": "demo::median::hfedcba9876543210"}) is None
    assert shown(top=False) is None
    assert shown(frame=None) is None
    # A `&mut` parameter only in the innermost frame: from an outer frame, a callee
    # may be midway through changing it. Shared references are read in any frame.
    session.parameters[("h0123456789abcdef", "values")]["type"] = "&mut [i32]"
    variable["type"] = "&mut [i32]"
    session.layouts = {**CONTAINERS, "&mut [i32]": SLICE_LAYOUT}
    assert shown() is None
    assert shown(innermost=True) == "[9]"


def test_describe_lists_the_reference_parameters_it_reads_from_memory():
    from peeky.cli import _describe_text

    text = _describe_text(
        {
            "language": "rust",
            "root": "/crate",
            "breakpoint_lines": {},
            "value_views": {
                VEC_NAME: "vec",
                "&[i32]": "slice",
                "&str": "unqualified",
                "core::option::Option<i32>": "option",
            },
            "mutable_types": "i32",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("read from memory")]
    assert line.endswith(f": &[i32], &{VEC_NAME}, &mut {VEC_NAME}")
    assert "reference parameters, owned" in line and "MIR proves" in line
    # And such fields of either (#156).
    assert "such fields of either (self.values)" in line


@pytest.mark.parametrize(
    "text, shown",
    [
        ("U+0062 U+0062 U'b'", "'b'"),
        ("U+0x00000062 U+0x00000062 U'b'", "'b'"),
        ("U+0x0000000a U+0x0000000a U'\\n'", "'\\n'"),
        ("U+0x0001f389 U+0x0001f389 U'🎉'", "'🎉'"),
        ("U+0x0000200d U+0x0000200d U'\u200d'", "'\\u{200d}'"),
        ("U+0027 U+0027 U'''", "'\\''"),
        ("U+0022 U+0022 U'\\\"'", "'\"'"),
        # Exact from Rust 1.96's own table: a spacing mark is printed, a combining
        # one escaped, U+1171E (Mn for Python, Mc for Unicode 17) printed.
        ("U+0903 U+0903 U'x'", "'\u0903'"),
        ("U+0301 U+0301 U'x'", "'\\u{301}'"),
        ("U+1171E U+1171E U'x'", "'\U0001171e'"),
        ("U+0x0000d800 U+0x0000d800 U'?'", None),
        ("U+0062 U+0063 U'b'", None),
        ("U+0x00110000 U+0x00110000 U'?'", None),
    ],
)
def test_rust_chars_print_as_rusts_debug_does_on_either_lldb(text, shown):
    from peeky.rust_symbols import lldb_char

    assert lldb_char(text, "char32_t") == shown
    assert lldb_char(text, "unsigned int") is None


def test_the_char_table_comes_from_the_pinned_rust():
    # The table is Rust's own `{:?}` output for every scalar value; it must be
    # regenerated (scripts/rust_char_table.py) whenever the pinned toolchain changes.
    from peeky.rust import RustTarget
    from peeky.rust_chars import ESCAPED, RUST_VERSION

    assert RUST_VERSION == RustTarget.__dataclass_fields__["rust_version"].default
    assert all(low <= high for low, high in ESCAPED)
    assert all(a[1] < b[0] for a, b in zip(ESCAPED, ESCAPED[1:]))


def composite_dump(element, slice_name):
    """A `&[T]` slice whose data pointer points at ``element`` (a structure DIE)."""
    tree = (
        "cu",
        "DW_TAG_compile_unit",
        [("DW_AT_name", '"src/main.rs/@/comp"')],
        [
            _struct(
                "slice",
                slice_name,
                "0x10",
                _member("sl_data", "data_ptr", "el_ptr", "0x00"),
                _member("sl_len", "length", "usize", "0x08"),
            ),
            _namespace("crate", "peeky_pagination", element),
            _base("char", "char", "0x04", "DW_ATE_UTF"),
            _base("usize", "usize", "0x08", "DW_ATE_unsigned"),
            _base("i64", "i64", "0x08", "DW_ATE_signed"),
            _base("bool", "bool", "0x01", "DW_ATE_boolean"),
            ("el_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "el"))], []),
            ("u_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "usize"))], []),
        ],
    )
    return _tree_dump(tree)


TUPLE = _struct(
    "el",
    "(char, usize)",
    "0x10",
    _member("t0", "__0", "char", "0x08"),
    _member("t1", "__1", "usize", "0x00"),
)
INTERVAL = _struct(
    "el",
    "Interval",
    "0x18",
    _member("f0", "start", "i64", "0x00"),
    _member("f1", "end", "i64", "0x08"),
    _member("f2", "open", "bool", "0x10"),
)


def test_composite_layouts_take_fields_and_shape_from_the_dwarf():
    from peeky.rust_symbols import container_layouts

    layouts = container_layouts(*composite_dump(TUPLE, "&[(char, usize)]"))
    assert layouts["&[(char, usize)]"] == {
        "kind": "slice",
        "size": 0x10,
        "data": 0,
        "length": 8,
        "element": "(char, usize)",
        "element_size": 0x10,
        "alignment": 8,
        "fields": [["__0", 8, "char"], ["__1", 0, "usize"]],
        "shape": "tuple",
        "shown": "(char, usize)",
    }
    # rustc declares a called inherent method (`fn width(&self)`) inside the struct.
    method = (
        "m",
        "DW_TAG_subprogram",
        [("DW_AT_name", '"width"'), ("DW_AT_declaration", "true")],
        [],
    )
    with_method = (*INTERVAL[:3], [*INTERVAL[3], method])
    layouts = container_layouts(*composite_dump(with_method, "&[peeky_pagination::Interval]"))
    layout = layouts["&[peeky_pagination::Interval]"]
    assert (layout["shape"], layout["shown"], layout["element_size"]) == ("struct", "Interval", 24)


@pytest.mark.parametrize(
    "element, name",
    [
        # Two fields at one offset.
        (
            _struct(
                "el",
                "(char, usize)",
                "0x10",
                _member("t0", "__0", "char", "0x00"),
                _member("t1", "__1", "usize", "0x00"),
            ),
            "&[(char, usize)]",
        ),
        # A field past the element's end.
        (
            _struct(
                "el",
                "Interval",
                "0x08",
                _member("f0", "start", "i64", "0x00"),
                _member("f1", "end", "i64", "0x08"),
            ),
            "&[peeky_pagination::Interval]",
        ),
        # A field that is not a qualified scalar (a pointer).
        (
            _struct("el", "Boxed", "0x08", _member("f0", "p", "u_ptr", "0x00")),
            "&[peeky_pagination::Boxed]",
        ),
        # A tuple whose name does not match its fields.
        (
            _struct(
                "el",
                "(char, u32)",
                "0x10",
                _member("t0", "__0", "char", "0x08"),
                _member("t1", "__1", "usize", "0x00"),
            ),
            "&[(char, u32)]",
        ),
        # Packed: a usize at an offset its alignment forbids.
        (
            _struct(
                "el",
                "Packed",
                "0x09",
                _member("f0", "flag", "bool", "0x00"),
                _member("f1", "n", "usize", "0x01"),
            ),
            "&[peeky_pagination::Packed]",
        ),
        # An enum: Rust's enums are structures with a variant part.
        (
            (
                "el",
                "DW_TAG_structure_type",
                [("DW_AT_name", '"Choice"'), ("DW_AT_byte_size", "0x08")],
                [("vp", "DW_TAG_variant_part", [], [])],
            ),
            "&[peeky_pagination::Choice]",
        ),
        # No fields.
        (
            (
                "el",
                "DW_TAG_structure_type",
                [("DW_AT_name", '"Unit"'), ("DW_AT_byte_size", "0x00")],
                [],
            ),
            "&[peeky_pagination::Unit]",
        ),
    ],
)
def test_composite_layouts_refuse_anything_unexpected(element, name):
    from peeky.rust_symbols import container_layouts

    # Recorded as unread, so a readable struct of the same path is not trusted either.
    assert container_layouts(*composite_dump(element, name))[name] == {"kind": "unread"}


def _composite_layout(shape, shown, fields, size, alignment):
    return {
        "kind": "slice",
        "size": 16,
        "data": 0,
        "length": 8,
        "element": shown,
        "element_size": size,
        "alignment": alignment,
        "fields": fields,
        "shape": shape,
        "shown": shown,
    }


def test_composite_elements_show_as_derived_debug_with_their_fields():
    from peeky.rust_symbols import container_view

    layouts = {
        "&[(char, usize)]": _composite_layout(
            "tuple", "(char, usize)", [["__0", 8, "char"], ["__1", 0, "usize"]], 16, 8
        ),
        "&[peeky_pagination::Interval]": _composite_layout(
            "struct",
            "Interval",
            [["start", 0, "i64"], ["end", 8, "i64"], ["open", 16, "bool"]],
            24,
            8,
        ),
        "&[comp::Point]": _composite_layout(
            "tuple-struct", "Point", [["__0", 0, "i32"], ["__1", 4, "u8"]], 8, 4
        ),
        "&[(i8)]": _composite_layout("tuple", "(i8)", [["__0", 0, "i8"]], 1, 1),
    }

    def shown(kind, raw, count):
        read = memory({"0x10": words(0x1000, count), "0x1000": raw})
        variable = {"name": "v", "type": kind, "memoryReference": "0x10"}
        return container_view(variable, read, layouts, optimized=False, parameter=kind)

    tuples = shown("&[(char, usize)]", words(3) + ord("a").to_bytes(8, "little"), 1)
    assert tuples["value"] == "[('a', 3)]"
    element = tuples["children"][-1]
    assert (element["name"], element["type"], element["value"]) == (
        "[0]",
        "(char, usize)",
        "('a', 3)",
    )
    assert element["children"] == [
        {"name": "__0", "type": "char", "value": "'a'"},
        {"name": "__1", "type": "usize", "value": "3"},
    ]
    interval = words(1) + (-10).to_bytes(8, "little", signed=True) + bytes([1]) + bytes(7)
    assert shown("&[peeky_pagination::Interval]", interval, 1)["value"] == (
        "[Interval { start: 1, end: -10, open: true }]"
    )
    point = ints(-3) + bytes([255, 0, 0, 0])
    assert shown("&[comp::Point]", point, 1)["value"] == "[Point(-3, 255)]"
    # rustc names a 1-tuple `(i8)`; Rust prints it `(7,)`.
    assert shown("&[(i8)]", bytes([7, 0xFF]), 2)["value"] == "[(7,), (-1,)]"
    # An element misaligned for its widest field is not read.
    read = memory({"0x10": words(0x1004, 1), "0x1004": interval})
    variable = {"name": "v", "type": "&[peeky_pagination::Interval]", "memoryReference": "0x10"}
    assert (
        container_view(
            variable, read, layouts, optimized=False, parameter="&[peeky_pagination::Interval]"
        )["rust_view"]
        == "opaque"
    )


def test_owned_composites_take_their_layout_from_the_mir_type_not_lldbs_name():
    # LLDB names Vec<Pair<i8>> `Vec<names::Pair<char>, …>`, which is the DWARF name of
    # Vec<Pair<char>>: the MIR's declared type picks the layout, LLDB's only confirms it.
    from peeky.rust_symbols import _composite_owned

    vec = "alloc::vec::Vec<{}, alloc::alloc::Global>".format
    layouts = {
        vec("names::Pair<i8>"): {},
        vec("names::Pair<char>"): {},
        vec("names::inner::Deep<f32>"): {},
        vec("(i8)"): {},
        vec("(char, usize)"): {},
    }
    shown = vec("names::Pair<char>")
    assert _composite_owned(shown, "std::vec::Vec<Pair<i8>>", layouts) == vec("names::Pair<i8>")
    assert _composite_owned(vec("names::Pair<char32_t>"), "std::vec::Vec<Pair<char>>", layouts) == (
        vec("names::Pair<char>")
    )
    assert _composite_owned(
        vec("names::inner::Deep<float>"), "std::vec::Vec<inner::Deep<f32>>", layouts
    ) == vec("names::inner::Deep<f32>")
    assert _composite_owned(vec("(i8)"), "std::vec::Vec<(i8,)>", layouts) == vec("(i8)")
    assert _composite_owned(vec("(char, usize)"), "std::vec::Vec<(char, usize)>", layouts)
    # No MIR type, a type the crate has no layout for, or an LLDB name of another shape.
    assert _composite_owned(shown, None, layouts) is None
    assert _composite_owned(shown, "std::vec::Vec<Pair<u8>>", layouts) is None
    assert _composite_owned(vec("names::Other<char>"), "std::vec::Vec<Pair<i8>>", layouts) is None
    # Two crates' worth of one name is ambiguous.
    twice = {**layouts, vec("other::Pair<i8>"): {}}
    assert _composite_owned(shown, "std::vec::Vec<Pair<i8>>", twice) is None


def test_composites_are_read_only_from_the_crate_and_never_beside_an_unreadable_twin():
    from peeky.rust_symbols import active_variant, container_layouts

    # Review of #98: `Range<usize>` passes the field checks but has its own Debug.
    std = (
        "el",
        "DW_TAG_structure_type",
        [("DW_AT_name", '"Range<usize>"'), ("DW_AT_byte_size", "0x10")],
        [_member("f0", "start", "usize", "0x00"), _member("f1", "end", "usize", "0x08")],
    )
    tree = (
        "cu",
        "DW_TAG_compile_unit",
        [("DW_AT_name", '"src/main.rs/@/comp"')],
        [
            _struct(
                "slice",
                "&[core::ops::range::Range<usize>]",
                "0x10",
                _member("sl_data", "data_ptr", "el_ptr", "0x00"),
                _member("sl_len", "length", "usize", "0x08"),
            ),
            _namespace("core", "core", _namespace("ops", "ops", _namespace("r", "range", std))),
            _base("usize", "usize", "0x08", "DW_ATE_unsigned"),
            ("el_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "el"))], []),
        ],
    )
    assert container_layouts(*_tree_dump(tree)) == {}
    # Review of #98: a struct in a function's body shares its path with a module's
    # (`crate::parse::Token`); when only one is readable, neither is read.
    readable = INTERVAL
    unreadable = _struct("el2", "Interval", "0x08", _member("g0", "p", "u_ptr", "0x00"))

    def twins(first, second):
        tree = (
            "cu",
            "DW_TAG_compile_unit",
            [("DW_AT_name", '"src/main.rs/@/comp"')],
            [
                _struct(
                    "slice",
                    "&[peeky_pagination::Interval]",
                    "0x10",
                    _member("sl_data", "data_ptr", "el_ptr", "0x00"),
                    _member("sl_len", "length", "usize", "0x08"),
                ),
                _struct(
                    "slice2",
                    "&[peeky_pagination::Interval]",
                    "0x10",
                    _member("sl2_data", "data_ptr", "el2_ptr", "0x00"),
                    _member("sl2_len", "length", "usize", "0x08"),
                ),
                _namespace("crate", "peeky_pagination", first, second),
                _base("i64", "i64", "0x08", "DW_ATE_signed"),
                _base("bool", "bool", "0x01", "DW_ATE_boolean"),
                _base("usize", "usize", "0x08", "DW_ATE_unsigned"),
                ("el_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "el"))], []),
                ("el2_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "el2"))], []),
                ("u_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "usize"))], []),
            ],
        )
        return container_layouts(*_tree_dump(tree))["&[peeky_pagination::Interval]"]

    assert twins(readable, unreadable) == {"kind": "unqualified"}
    alone = _struct("el", "Interval", "0x08", _member("g0", "p", "u_ptr", "0x00"))
    alone = container_layouts(*composite_dump(alone, "&[peeky_pagination::Interval]"))
    # Recorded for previews too (#106), under a key no type name has.
    assert alone == {
        "&[peeky_pagination::Interval]": {"kind": "unread"},
        "struct:peeky_pagination::Interval": {"kind": "unread"},
    }
    # Such a container is never mistaken for an enum.
    variable = {"name": "v", "type": "&[peeky_pagination::Interval]", "variablesReference": 1}
    assert active_variant(variable, None, alone) is None


def struct_dump(*structs, roots=()):
    """A unit of the crate `peeky_pagination` declaring ``structs`` (and ``roots``, such
    as tuples, outside any namespace)."""
    from peeky.rust_symbols import container_layouts

    tree = (
        "cu",
        "DW_TAG_compile_unit",
        [("DW_AT_name", '"src/main.rs/@/comp"')],
        [
            _namespace("crate", "peeky_pagination", *structs),
            *roots,
            _namespace("core", "core", _namespace("r", "range", RANGE)),
            _base("char", "char", "0x04", "DW_ATE_UTF"),
            _base("usize", "usize", "0x08", "DW_ATE_unsigned"),
            _base("i64", "i64", "0x08", "DW_ATE_signed"),
            _base("i8", "i8", "0x01", "DW_ATE_signed"),
            _base("u8", "u8", "0x01", "DW_ATE_unsigned"),
            _base("bool", "bool", "0x01", "DW_ATE_boolean"),
            ("u_ptr", "DW_TAG_pointer_type", [("DW_AT_type", ("ref", "usize"))], []),
        ],
    )
    return container_layouts(*_tree_dump(tree))


RANGE = _struct(
    "range",
    "Range<usize>",
    "0x10",
    _member("r0", "start", "usize", "0x00"),
    _member("r1", "end", "usize", "0x08"),
)


def test_struct_and_tuple_layouts_are_kept_for_previews():
    # #106: the crate's structs and every tuple whose fields are qualified scalars.
    from peeky.rust_symbols import STRUCT_PREFIX

    pair = _struct(
        "pair",
        "Pair<i8>",
        "0x02",
        _param("pair_t", "i8"),
        _member("p0", "a", "i8", "0x00"),
        _member("p1", "b", "i8", "0x01"),
    )
    point = _struct(
        "point",
        "Point",
        "0x10",
        _member("q0", "__0", "i64", "0x00"),
        _member("q1", "__1", "u8", "0x08"),
    )
    unit = _struct("unit", "Marker", "0x00")
    tuple_ = _struct(
        "tup",
        "(i64, char)",
        "0x10",
        _member("t0", "__0", "i64", "0x00"),
        _member("t1", "__1", "char", "0x08"),
    )
    layouts = struct_dump(INTERVAL, pair, point, unit, roots=(tuple_,))
    shapes = {
        name.removeprefix(STRUCT_PREFIX): (layout["kind"], layout.get("shape"), layout["shown"])
        for name, layout in layouts.items()
    }
    assert shapes == {
        "peeky_pagination::Interval": ("composite", "struct", "Interval"),
        "peeky_pagination::Pair<i8>": ("composite", "struct", "Pair"),
        "peeky_pagination::Point": ("composite", "tuple-struct", "Point"),
        "peeky_pagination::Marker": ("unit", None, "Marker"),
        "(i64, char)": ("composite", "tuple", "(i64, char)"),
    }
    # The standard library's `Range` has its own Debug (`1..5`): never kept.
    assert not any("Range" in name for name in layouts)


@pytest.mark.parametrize(
    "element",
    [
        # A field that is not a qualified scalar (a pointer), packed, and an enum.
        _struct("el2", "Interval", "0x08", _member("g0", "p", "u_ptr", "0x00")),
        _struct(
            "el2",
            "Interval",
            "0x09",
            _member("g0", "open", "bool", "0x00"),
            _member("g1", "n", "usize", "0x01"),
        ),
        (
            "el2",
            "DW_TAG_structure_type",
            [("DW_AT_name", '"Interval"'), ("DW_AT_byte_size", "0x08")],
            [("vp2", "DW_TAG_variant_part", [], [])],
        ),
    ],
)
def test_structs_that_are_not_plain_scalars_are_never_previewed(element):
    from peeky.rust_symbols import STRUCT_PREFIX

    key = STRUCT_PREFIX + "peeky_pagination::Interval"
    assert struct_dump(element)[key] == {"kind": "unread"}
    # Nor is a readable struct sharing its path (one in a function's body).
    assert struct_dump(INTERVAL, element)[key] == {"kind": "unqualified"}


STRUCTS = {
    "struct:peeky_pagination::Span": {
        "kind": "composite",
        "element_size": 16,
        "alignment": 8,
        "fields": [["start", 0, "usize"], ["end", 8, "usize"]],
        "shape": "struct",
        "shown": "Span",
    },
    "struct:peeky_pagination::Pair<i8>": {
        "kind": "composite",
        "element_size": 2,
        "alignment": 1,
        "fields": [["a", 0, "i8"], ["b", 1, "i8"]],
        "shape": "struct",
        "shown": "Pair",
    },
    "struct:peeky_pagination::Pair<char>": {
        "kind": "composite",
        "element_size": 8,
        "alignment": 4,
        "fields": [["a", 0, "char"], ["b", 4, "char"]],
        "shape": "struct",
        "shown": "Pair",
    },
    "struct:(i8)": {
        "kind": "composite",
        "element_size": 1,
        "alignment": 1,
        "fields": [["__0", 0, "i8"]],
        "shape": "tuple",
        "shown": "(i8)",
    },
    "struct:peeky_pagination::Marker": {"kind": "unit", "shown": "Marker"},
    "struct:peeky_pagination::Twin": {"kind": "unqualified"},
    "struct:peeky_pagination::Named": {"kind": "unread"},
}


def test_a_preview_takes_its_layout_from_the_mir_type_not_lldbs_name():
    from peeky.rust_symbols import struct_name

    # LLDB names Pair<i8> `Pair<char>`, which is the DWARF name of Pair<char>.
    shown = "peeky_pagination::Pair<char>"
    assert struct_name("Pair<i8>", shown, STRUCTS) == "peeky_pagination::Pair<i8>"
    assert struct_name("Pair<char>", "peeky_pagination::Pair<char32_t>", STRUCTS) == (
        "peeky_pagination::Pair<char>"
    )
    assert struct_name("(i8,)", "(i8)", STRUCTS) == "(i8)"
    # No MIR type, none of the crate's, or LLDB's name for another type.
    assert struct_name(None, shown, STRUCTS) is None
    assert struct_name("Pair<u8>", shown, STRUCTS) is None
    assert struct_name("Span", "peeky_pagination::Other", STRUCTS) is None
    assert struct_name("Span", "peeky_pagination::Span *", STRUCTS) is None


def test_a_preview_finds_its_struct_by_the_path_the_mir_prints():
    # Review 5 of #106: the MIR names a type from its nearest uniquely named ancestor,
    # and a raw identifier with `r#`.
    from peeky.rust_symbols import struct_name

    point = {"kind": "composite", "shape": "tuple-struct"}
    paths = ("Point", "main::Point", "geo::shapes::Point", "other::Point", "match::Kw", "type")
    structs = {f"struct:demo::{path}": point for path in paths}

    def found(declared, path):
        return struct_name(declared, f"demo::{path}", structs) == f"demo::{path}"

    assert found("shapes::Point", "geo::shapes::Point") and found("other::Point", "other::Point")
    # A name alone, or a path that is a suffix of another's, is crate-relative.
    assert found("Point", "Point") and found("main::Point", "main::Point")
    assert found("r#match::Kw", "match::Kw") and found("r#type", "type")
    twins = {f"struct:demo::{path}": point for path in ("a::shapes::P", "b::shapes::P")}
    assert struct_name("shapes::P", "demo::a::shapes::P", twins) is None
    # LLDB's name still decides between a suffix and another type (`_spelled`).
    assert struct_name("shapes::Point", "demo::other::Point", structs) is None


def preview(variable, blocks, **proof):
    from peeky.rust_symbols import struct_view

    return struct_view(
        {"name": "s", "memoryReference": "0x10", **variable}, memory(blocks), STRUCTS, **proof
    )


def test_structs_and_tuples_preview_as_derived_debug_with_their_fields():
    span = {"type": "peeky_pagination::Span", "value": "peeky_pagination::Span @ 0x10"}
    shown = preview(span, {"0x10": words(2, 3)}, owned=True, declared="Span")
    assert shown["value"] == "Span { start: 2, end: 3 }" and shown["rust_view"] == "memory"
    assert shown["children"] == [
        {"name": "start", "type": "usize", "value": "2"},
        {"name": "end", "type": "usize", "value": "3"},
    ]
    pair = {"type": "peeky_pagination::Pair<char>"}
    assert preview(pair, {"0x10": bytes([1, 0xFE])}, owned=True, declared="Pair<i8>")["value"] == (
        "Pair { a: 1, b: -2 }"
    )
    single = {"type": "(i8)", "value": "(i8) @ 0x10"}
    assert preview(single, {"0x10": bytes([7])}, owned=True, declared="(i8,)")["value"] == "(7,)"
    marker = {"type": "peeky_pagination::Marker"}
    assert preview(marker, {}, owned=True, declared="Marker")["value"] == "Marker"
    # A reference parameter: read through the pointer it holds, which LLDB shows.
    reference = {"type": "peeky_pagination::Span *", "value": "0x0000000000001000"}
    blocks = {"0x10": words(0x1000), "0x1000": words(4, 9)}
    shown = preview(reference, blocks, parameter="&mut peeky_pagination::Span")
    assert shown["value"] == "Span { start: 4, end: 9 }"


def test_a_preview_needs_a_proof_a_plain_layout_and_agreeing_addresses():
    span = {"type": "peeky_pagination::Span", "value": "peeky_pagination::Span @ 0x10"}
    blocks = {"0x10": words(2, 3)}
    # Not proven initialized (moved, not yet assigned, a caller's frame).
    assert preview(span, blocks, declared="Span") is None
    # Same-named structs, one with other fields, a std struct: never read.
    for name in ("Twin", "Named", "Range<usize>"):
        variable = {"type": f"peeky_pagination::{name}"}
        assert preview(variable, blocks, owned=True, declared=name) is None
    # LLDB's address for the value is not where the debugger reads it.
    moved = {**span, "value": "peeky_pagination::Span @ 0x20"}
    assert preview(moved, blocks, owned=True, declared="Span") is None
    # A reference whose pointer the debugger shows differently, null or misaligned.
    reference = {"type": "peeky_pagination::Span *", "value": "0x0000000000001000"}
    for target in (0x2000, 0, 0x1004):
        blocks = {"0x10": words(target), "0x1000": words(4, 9), "0x1004": words(4, 9)}
        shown = {**reference, "value": f"0x{target:016x}"} if target != 0x2000 else reference
        assert preview(shown, blocks, parameter="&peeky_pagination::Span") is None
    # A memory read the debugger refuses, or past the view's budget, leaves it raw.
    from peeky.adapters import ViewBudgetExhausted
    from peeky.rust_symbols import struct_view

    def refuse(address, count):
        raise ViewBudgetExhausted("budget")

    variable = {"name": "s", "memoryReference": "0x10", **span}
    assert struct_view(variable, refuse, STRUCTS, owned=True, declared="Span") is None
    # A bool that is neither 0 nor 1 is not a value the program could hold.
    flags = {
        "struct:peeky_pagination::Flag": {
            "kind": "composite",
            "element_size": 1,
            "alignment": 1,
            "fields": [["on", 0, "bool"]],
            "shape": "struct",
            "shown": "Flag",
        }
    }
    variable = {"name": "f", "type": "peeky_pagination::Flag", "memoryReference": "0x10"}
    read = memory({"0x10": bytes([2])})
    assert struct_view(variable, read, flags, owned=True, declared="Flag") is None


@pytest.mark.parametrize(
    "name, shown, spelled",
    [
        ("chars::Pair<i8>", "chars::Pair<char>", True),
        ("chars::Pair<i8>", "chars::Pair<char32_t>", False),
        ("chars::Pair<char>", "chars::Pair<char32_t>", True),
        ("chars::Pair<char>", "chars::Pair<char>", False),
        ("chars::Pair<i64>", "chars::Pair<unsigned long>", False),
        ("chars::Pair<u16> *", "chars::Pair<unsigned short> *", True),
        ("(i8, i8)", "(i8, i8)", True),
        ("(i8, i8)", "(char, char)", False),
        ("(i64, char) *", "(i64, char) *", True),
        ("chars::Span", "chars::Span *", False),
    ],
)
def test_lldbs_name_must_spell_the_layouts_type(name, shown, spelled):
    # Review of #106: LLDB names a generic's primitive arguments the C way, and a tuple
    # as the DWARF does; `_lossy` let `Pair<char32_t>` pass for `Pair<i8>`.
    from peeky.rust_symbols import _spelled

    assert _spelled(name, shown) is spelled


def test_tuple_structs_are_unread_where_a_source_names_a_field_like_theirs():
    # Review of #106: `struct Num { __0: u32 }` has the DWARF of `struct Num(u32)`.
    from peeky.rust_symbols import without_numbered_structs

    layouts = {
        "struct:peeky_pagination::Num": {"kind": "composite", "shape": "tuple-struct"},
        "struct:peeky_pagination::Span": {"kind": "composite", "shape": "struct"},
        "&[peeky_pagination::Num]": {"kind": "slice", "shape": "tuple-struct"},
    }
    unread = {
        "struct:peeky_pagination::Num": {"kind": "unread"},
        "struct:peeky_pagination::Span": layouts["struct:peeky_pagination::Span"],
        "&[peeky_pagination::Num]": {"kind": "unread"},
    }
    plain = (
        'struct Two(u32, u32);\n// __0: a comment\nlet s = "__1: text";\nlet n = 1__0;\n'
        '#[cfg(feature = "x")]\nfn f() {}\nlet t = "feature(x)"; // #![feature(y)]\n'
    )
    assert without_numbered_structs(layouts, [plain]) == layouts
    # Fields a macro declares from the names it is given (review 2).
    generated = without_numbered_structs(layouts, [plain, "fields!(Num, __0, __1);\n"])
    assert generated["&[peeky_pagination::Num]"] == {"kind": "unread"}
    declared = "struct Num {\n    __0: u32,\n}\n"
    assert without_numbered_structs(layouts, [plain, declared]) == unread
    # Review 5: a nightly feature gate (`${concat(__, "0")}` writes no `__0`), however
    # spelled, and a source not lexed.
    for gate in (
        "#![feature(macro_metavar_expr_concat)]\n",
        "#![cfg_attr(all(), feature /* x */ (macro_metavar_expr_concat))]\n",
        "#![r#feature(macro_metavar_expr_concat)]\n",
        "#![feature\n(macro_metavar_expr_concat)]\n",
        "#![feature\u200e(macro_metavar_expr_concat)]\n",
        "#![feature\u200f (macro_metavar_expr_concat)]\n",
        "fn main() { /* not closed\n",
    ):
        assert without_numbered_structs(layouts, [plain, gate]) == unread, gate
    # Review 6: Cargo.toml and Cargo.lock, which `include!` reads as Rust, are searched as
    # plain text: they need not lex as Rust, and a `__0` in their strings counts too.
    manifest = '[package]\nname = "demo" # don\'t\nversion = "0.1.0"\n'
    assert without_numbered_structs(layouts, [plain], [manifest]) == layouts
    for trick in (
        '#[doc = ""] fields!(__0, __1); ignore! {\n' + manifest + "#}\n",
        manifest + 'description = "__0"\n',
    ):
        assert without_numbered_structs(layouts, [plain], [manifest, trick]) == unread, trick
    # A feature gate is the crate root's alone, never an included file's.
    assert without_numbered_structs(layouts, [plain], ["# feature(x)\n"]) == layouts


def test_a_packed_struct_is_never_previewed_even_when_aligned():
    # Review of #106: `#[repr(packed)] struct Pk { a: u32, b: u32 }` has aligned fields.
    from peeky.rust_symbols import STRUCT_PREFIX

    packed = (
        "pk",
        "DW_TAG_structure_type",
        [("DW_AT_name", '"Interval"'), ("DW_AT_byte_size", "0x10"), ("DW_AT_alignment", "1")],
        [_member("k0", "start", "i64", "0x00"), _member("k1", "end", "i64", "0x08")],
    )
    key = STRUCT_PREFIX + "peeky_pagination::Interval"
    assert struct_dump(packed)[key] == {"kind": "unread"}
    aligned = (*packed[:2], [*packed[2][:2], ("DW_AT_alignment", "8")], packed[3])
    assert struct_dump(aligned)[key]["kind"] == "composite"


def blocks_dump(*, ranges=True):
    """`kay` (a `println!` after `let k = r.x;`) as llvm-dwarfdump shows it: the macro's
    `args` scopes nest in `k`'s block; a method's code completes its declaration."""
    lines = [
        *entry(0x0B, 0, "DW_TAG_compile_unit", ("DW_AT_name", json.dumps(APP))),
        *entry(0x20, 1, "DW_TAG_namespace", ("DW_AT_name", '"chars"')),
        *entry(
            0x30,
            2,
            "DW_TAG_subprogram",
            ("DW_AT_low_pc", "0x0000000100000e0c"),
            ("DW_AT_high_pc", "0x0000000100000ea4"),
            ("DW_AT_linkage_name", '"_ZN5chars3kay17h8491af1d40105d89E"'),
            ("DW_AT_name", '"kay"'),
        ),
        *entry(0x40, 3, "DW_TAG_formal_parameter", ("DW_AT_name", '"v"')),
        *entry(0x48, 3, "DW_TAG_lexical_block", ("DW_AT_ranges", "0x00000090")),
        "                       [0x0000000100000e38, 0x0000000100000e88)",
        "                       [0x0000000100000e98, 0x0000000100000ea4))",
        *entry(0x50, 4, "DW_TAG_variable", ("DW_AT_name", '"r"')),
        *entry(
            0x58,
            4,
            "DW_TAG_lexical_block",
            *(
                [("DW_AT_low_pc", "0x0000000100000e44"), ("DW_AT_high_pc", "0x0000000100000e88")]
                if ranges
                else [("DW_AT_low_pc", "0x0000000100000e44"), ("DW_AT_high_pc", "0x00000000")]
            ),
        ),
        *entry(0x60, 5, "DW_TAG_variable", ("DW_AT_name", '"k"')),
        *entry(
            0x68,
            5,
            "DW_TAG_lexical_block",
            ("DW_AT_low_pc", "0x0000000100000e50"),
            ("DW_AT_high_pc", "0x0000001c"),
        ),
        *entry(0x70, 6, "DW_TAG_variable", ("DW_AT_name", '"args"')),
        *entry(
            0x78,
            6,
            "DW_TAG_inlined_subroutine",
            ("DW_AT_low_pc", "0x0000000100000e58"),
            ("DW_AT_high_pc", "0x0000000100000e5c"),
        ),
        *entry(0x80, 7, "DW_TAG_variable", ("DW_AT_name", '"inner"')),
        *null(0x88, 7),
        *null(0x89, 6),
        *null(0x8A, 5),
        *null(0x8B, 4),
        *null(0x8C, 3),
        *entry(0x90, 2, "DW_TAG_structure_type", ("DW_AT_name", '"Pt"')),
        *entry(
            0x98,
            3,
            "DW_TAG_subprogram",
            ("DW_AT_linkage_name", '"_ZN5chars2Pt3get17h0123456789abcdefE"'),
            ("DW_AT_name", '"get"'),
            ("DW_AT_declaration", "true"),
        ),
        *null(0xA0, 3),
        *entry(
            0xA8,
            2,
            "DW_TAG_subprogram",
            ("DW_AT_low_pc", "0x0000000100001000"),
            ("DW_AT_high_pc", "0x00000020"),
            ("DW_AT_specification", '0x00000098 "_ZN5chars2Pt3get17h0123456789abcdefE"'),
        ),
        *entry(0xB0, 3, "DW_TAG_formal_parameter", ("DW_AT_name", '"self"')),
        *null(0xB8, 3),
        *null(0xB9, 2),
        *null(0xBA, 1),
    ]
    return "\n".join(lines) + "\n"


def test_a_function_s_lexical_blocks_name_their_variables_and_code(tmp_path):
    # #115: which DWARF lexical block a stop's address is in tells a macro's own stops
    # apart (`println!`'s `args`); an inlined call's own blocks are its callee's.
    (root,) = parse_dies(blocks_dump(), units=False)
    found = code_blocks(root, RustUnit(0, 0x0B, 0x1000, 0, APP))
    kay = found["h8491af1d40105d89"]
    assert [(block.parent, block.names, block.inlined) for block in kay] == [
        (None, ("v",), False),
        (0, ("r",), False),
        (1, ("k",), False),
        (2, ("args",), False),
        (3, (), True),
    ]
    assert kay[1].ranges == ((0x100000E38, 0x100000E88), (0x100000E98, 0x100000EA4))
    # DWARF 4's high_pc may be the code's size.
    assert kay[3].ranges == ((0x100000E50, 0x100000E6C),)
    # A method's code takes its declaration's linkage name.
    assert found["h0123456789abcdef"][0].names == ("self",)
    # A size is told from an end by its digits, not by being below the start.
    low = blocks_dump().replace("0x0000000100001000", "0x0000000000000100")
    (root,) = parse_dies(low.replace("0x00000020", "0x00000200"), units=False)
    found = code_blocks(root, RustUnit(0, 0x0B, 0x1000, 0, APP))
    assert found["h0123456789abcdef"][0].ranges == ((0x100, 0x300),)
    # A block without code gives its function no blocks: none is told apart.
    (root,) = parse_dies(blocks_dump(ranges=False), units=False)
    assert code_blocks(root, RustUnit(0, 0x0B, 0x1000, 0, APP))["h8491af1d40105d89"] == ()


def test_describe_lists_the_locals_no_stop_can_change_per_function():
    # #138: the `mutable:` line names each function's locals kept in a register.
    from peeky.cli import _describe_text

    text = _describe_text(
        {
            "language": "rust",
            "root": "/crate",
            "breakpoint_lines": {},
            "scopes": [
                {"scope": "limiter::allow", "lines": [9, 20], "names": ["limit", "now", "w"]}
                | {"never_settable": {"limit": "register", "now": "register"}}
                | {"source": "src/main.rs"},
                {"scope": "app::aw::{{closure}}", "lines": [86, 94], "names": ["k"]}
                | {"never_settable": {"k": "async state"}, "source": "src/main.rs"},
                {"scope": "limiter::main", "lines": [22, 35], "names": ["w"]}
                | {"source": "src/main.rs"},
                {"scope": "bind::shadow", "lines": [3, 9], "names": ["n", "x"]}
                | {
                    "never_settable": {
                        "n": "register",
                        "x": "another binding in a register (line 4)",
                    }
                }
                | {"source": "src/main.rs"},
                {"scope": "bind::other::far", "lines": [3, 5], "names": ["a", "y"]}
                | {"never_settable": {"a": "inlined", "y": "inlined"}, "source": "src/other.rs"},
            ],
            "mutable_types": "integer root locals",
            "commands": {},
            "timing": {"advice": "fast"},
        }
    )
    [line] = [line for line in text.splitlines() if line.startswith("mutable:")]
    assert "COOKBOOK 6.6): limiter::allow: limit, now; bind::shadow: n. Never settable, a" in line
    # Review round 3: as the rows say, the binding to black-box, and inlining.
    assert "every one in memory, COOKBOOK 6.6): bind::shadow: x (line 4). Never" in line
    assert "change the value in the caller): bind::other::far: a, y. Never settable, held" in line
    assert line.endswith("a local after the `.await`): app::aw::{{closure}}: k"), line
