"""Rust target/session contracts and worker value-view hooks without a real toolchain."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from peeky.adapters import PythonAdapter
from peeky.build import SourceEdit
from peeky.native import NativeSession
from peeky.rust import RustSession, RustTarget, cargo_artifacts
from peeky.rust_symbols import UNVERIFIED, active_variant
from peeky.worker import DebugWorker, load_rust_target

ROOT = Path(__file__).resolve().parents[2]
OPTION = "core::option::Option<i32>"
INNER = f"{OPTION}::{OPTION}$Inner"
LAYOUTS = {
    OPTION: {
        "kind": "option",
        "discriminant": "u32",
        "variants": [[0, "None"], [1, "Some"]],
        "payload": "i32",
    },
    "core::result::Result<i32, E>": {"kind": "unqualified"},
}
HASH = "0123456789abcdef"
BINARY = f"target/debug/deps/peeky_pagination-{HASH}"


def toolchain(tmp_path, *, sysroot=True, split=False):
    bin_dir = tmp_path / "toolchain/bin"
    bin_dir.mkdir(parents=True)
    if sysroot:
        (tmp_path / "toolchain/lib/rustlib").mkdir(parents=True)
    tools = {}
    for name in ("cargo", "rustc", "lldb-dap", "llvm-dwarfdump", "cc", "dsymutil"):
        path = (tmp_path / "other" if split and name == "cargo" else bin_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
        tools[name] = str(path)
    return {
        "sources": ("src/main.rs",),
        "tests": ("tests/pagination.rs",),
        "cargo": tools["cargo"],
        "rustc": tools["rustc"],
        "adapter": tools["lldb-dap"],
        "dwarfdump": tools["llvm-dwarfdump"],
        "linker": tools["cc"],
        "dsymutil": tools["dsymutil"],
    }


def test_rust_target_accepts_explicit_sysroot_toolchain(tmp_path):
    target = RustTarget(**toolchain(tmp_path))
    assert target.verification_inputs == (
        "Cargo.toml",
        "Cargo.lock",
        "src/main.rs",
        "tests/pagination.rs",
    )
    assert target.verification_sources == ("src/main.rs",)
    identity = target.target_identity
    assert identity["language"] == "rust" and identity["rust_version"] == "1.96.0"
    assert "dwarf-version=4" in identity["rustflags"]
    assert identity["profile"]["CARGO_INCREMENTAL"] == "0"
    with pytest.raises(ValueError, match="line breakpoints"):
        target.validate_breakpoints([{"line": 3, "condition": "x"}])


@pytest.mark.parametrize(
    "change",
    [
        {"cargo": "cargo"},
        {"rustc": "relative/rustc"},
        {"sources": ["src/main.rs"]},
        {"sources": ("src/main.c",)},
        {"tests": ()},
        {"sources": ("../main.rs",)},
        {"arguments": ("a\x00b",)},
        {"rust_version": "stable"},
        {"adapter": "lldb-dap"},
    ],
)
def test_rust_target_rejects_unsafe_configuration(tmp_path, change):
    with pytest.raises((ValueError, OSError)):
        RustTarget(**{**toolchain(tmp_path), **change})


@pytest.mark.parametrize("options", [{"sysroot": False}, {"split": True}])
def test_rust_target_rejects_proxy_or_mixed_toolchains(tmp_path, options):
    with pytest.raises(ValueError, match="sysroot"):
        RustTarget(**toolchain(tmp_path, **options))


def example(tmp_path):
    root = tmp_path / "app"
    for name in ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes((ROOT / "examples/rust" / name).read_bytes())
    return root


@pytest.mark.parametrize(
    "program, edit",
    [
        ("tests/pagination.rs", None),
        ("src/main.rs", SourceEdit("tests/pagination.rs", "1, 3, 8", "1, 3, 9")),
        ("src/main.rs", SourceEdit("Cargo.toml", "0.1.0", "0.1.1")),
    ],
)
def test_rust_prepare_rejects_undeclared_programs_and_frozen_edits(tmp_path, program, edit):
    root = example(tmp_path)
    target = RustTarget(**toolchain(tmp_path))
    with pytest.raises(ValueError, match="declared"):
        target.prepare(root / program, root, edit=edit)


def test_cargo_artifacts_select_one_hashed_binary_and_symbols(monkeypatch):
    monkeypatch.setattr("peeky.rust.sys.platform", "linux")
    assert cargo_artifacts([BINARY, BINARY + ".d"], "peeky_pagination") == (BINARY, BINARY)
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    symbols = f"{BINARY}.dSYM/Contents/Resources/DWARF/peeky_pagination-{HASH}"
    names = [BINARY, BINARY + ".d", f"{BINARY}.dSYM/Contents/Info.plist", symbols]
    assert cargo_artifacts(names, "peeky_pagination") == (BINARY, symbols)


@pytest.mark.parametrize(
    "platform, names",
    [
        ("linux", [BINARY, BINARY.replace(HASH, "f" * 16)]),
        ("linux", [BINARY, "target/debug/deps/libother.rlib"]),
        ("linux", [BINARY[:-1]]),
        ("linux", [BINARY, f"{BINARY}.dSYM/Contents/Info.plist"]),
        ("linux", [BINARY.replace("peeky_pagination", "other")]),
        ("darwin", [BINARY, BINARY + ".d"]),
        ("darwin", [BINARY, f"{BINARY}.dSYM/Contents/Resources/DWARF/other"]),
    ],
)
def test_cargo_artifacts_reject_missing_duplicate_or_unexpected_outputs(
    monkeypatch, platform, names
):
    monkeypatch.setattr("peeky.rust.sys.platform", platform)
    with pytest.raises(ValueError):
        cargo_artifacts(names, "peeky_pagination")


def bare_session():
    session = object.__new__(RustSession)
    session._layouts_data = json.dumps(LAYOUTS)
    return session


@pytest.mark.parametrize(
    "variable",
    [
        {"name": "flag", "type": "unsigned int", "variablesReference": 0},
        {"name": "some", "type": OPTION, "variablesReference": 0},
        {"name": "wide", "type": "long", "variablesReference": 0},
    ],
)
def test_rust_mutation_is_limited_to_i32_locals(variable):
    with pytest.raises(ValueError, match="i32"):
        bare_session().validate_variable(variable, "1")
    bare_session().validate_variable({"name": "start", "type": "int"}, "0")


def test_rust_launch_disables_symbol_file_scripts(monkeypatch):
    monkeypatch.setattr(NativeSession, "launch_args", lambda self, program, root: {"a": 1})
    assert bare_session().launch_args("p", "r") == {
        "a": 1,
        "initCommands": ["settings set target.load-script-from-symbol-file false"],
    }


def test_load_rust_target_requires_explicit_known_fields(tmp_path):
    config = {**toolchain(tmp_path)}
    config["sources"], config["tests"] = list(config["sources"]), list(config["tests"])
    path = tmp_path / "rust.json"
    path.write_text(json.dumps(config))
    assert isinstance(load_rust_target(path), RustTarget)
    for change in ({"command": "x"}, {"sources": "src/main.rs"}):
        path.write_text(json.dumps({**config, **change}))
        with pytest.raises(ValueError):
            load_rust_target(path)
    path.write_text(json.dumps({k: v for k, v in config.items() if k != "linker"}))
    with pytest.raises(ValueError, match="explicit tool"):
        load_rust_target(path)


def raw(name, kind, ref=0, value=""):
    return {"name": name, "type": kind, "value": value, "variablesReference": ref}


class Backend:
    def __init__(self):
        self.calls = []
        self.roots = [
            raw("some", OPTION, 10, "Option @ 0x1"),
            raw("result", "core::result::Result<i32, E>", 20, "Result @ 0x2"),
            raw("holder", "app::Holder", 30, "Holder @ 0x3"),
            raw("start", "int", 0, "3"),
        ]
        self.children = {
            10: [raw("$variants$", INNER, 11)],
            11: [
                raw("$variant$0", INNER + "::None$Variant", 12),
                raw("$variant$1", INNER + "::Some$Variant", 13),
            ],
            12: [raw("$discr$", "unsigned int", 0, "1"), raw("value", f"{OPTION}::None<int>", 14)],
            13: [raw("$discr$", "unsigned int", 0, "1"), raw("value", f"{OPTION}::Some<int>", 15)],
            14: [raw("__0", "int", 0, "999")],
            15: [raw("__0", "int", 0, "3")],
            20: [raw("$variants$", "inner", 21)],
            30: [raw("unknown", "app::Unknown", 31)],
            31: [raw("$variants$", "app::Unknown$Inner", 32), raw("$discr$", "u8", 0, "0")],
        }

    def request(self, operation, args):
        self.calls.append((operation, deepcopy(args)))
        if operation == "stackTrace":
            return {
                "stackFrames": [
                    {"id": 1, "line": 2, "source": {"path": "/p/main.rs"}, "moduleId": "m"}
                ]
            }
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": 7}
                ]
            }
        if operation == "variables":
            ref = args["variablesReference"]
            return {"variables": deepcopy(self.roots if ref == 7 else self.children[ref])}
        raise AssertionError(operation)

    def close(self):
        pass


class Runtime:
    def __init__(self):
        self.layouts = LAYOUTS

    def assert_current(self):
        pass

    def validate_stop(self, frame):
        pass

    def validate_debugger(self, dap, frame):
        pass

    def local_scopes(self, scopes):
        return PythonAdapter().local_scopes(scopes)

    def view_variable(self, variable, fetch):
        return active_variant(variable, fetch, self.layouts)

    validate_children = RustSession.validate_children

    def close(self):
        pass


@pytest.fixture
def worker(tmp_path):
    instance = DebugWorker(tmp_path)
    instance.dap, instance.runtime = Backend(), Runtime()
    instance.session, instance.stop_id, instance.state = "session", "stop", "stopped"
    instance.thread_id = 1
    instance.capabilities = {"supports_set_variable": True}
    return instance


def call(worker, operation, **kwargs):
    return worker.dispatch(operation, {"session_id": "session", "stop_id": "stop", **kwargs})


def test_worker_exposes_active_variant_views_without_raw_handles(worker):
    variables = {v["name"]: v for v in call(worker, "inspect")["scopes"][0]["variables"]}
    assert variables["some"] == {
        "name": "some",
        "value": "Some(3)",
        "variablesReference": 0,
        "view": "active-variant",
        "type": OPTION,
    }
    assert variables["result"]["view"] == "opaque"
    assert variables["result"]["variablesReference"] == 0
    assert variables["holder"]["variablesReference"] > 0
    assert variables["start"]["value"] == "3" and "view" not in variables["start"]
    read = [args["variablesReference"] for op, args in worker.dap.calls if op == "variables"]
    assert 14 not in read and 20 not in read and worker.view_fetches == 5


def test_worker_refuses_raw_storage_of_enums_missing_from_layouts(worker):
    variables = {v["name"]: v for v in call(worker, "inspect")["scopes"][0]["variables"]}
    holder = call(worker, "expand", handle=variables["holder"]["variablesReference"])
    unknown = holder["variables"][0]
    assert unknown["variablesReference"] > 0 and "view" not in unknown
    with pytest.raises(ValueError, match="Raw Rust enum storage"):
        call(worker, "expand", handle=unknown["variablesReference"])


def test_worker_value_view_budget_degrades_to_opaque(worker):
    worker.dap.roots = [raw(f"some{index}", OPTION, 10) for index in range(30)]
    views = call(worker, "inspect")["scopes"][0]["variables"]
    assert [view["value"] for view in views[:25]] == ["Some(3)"] * 25
    assert {view["value"] for view in views[26:]} == {UNVERIFIED}
    assert all(view["variablesReference"] == 0 for view in views)
    assert worker.view_fetches == 128
    call(worker, "inspect")
    assert worker.view_fetches == 128
