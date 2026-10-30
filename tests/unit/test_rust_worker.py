"""Rust target/session contracts and worker value-view hooks without a real toolchain."""

import json
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from peeky.adapters import PythonAdapter
from peeky.agent import BUILD_RETAINED
from peeky.build import SourceEdit
from peeky.native import NativeSession
from peeky.rust import (
    RustSession,
    RustTarget,
    cargo_artifacts,
    compiler_diagnostic,
    libtest_summary,
)
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
        # No integration test is fine (the checker alone verifies); a list is not.
        {"tests": ["tests/pagination.rs"]},
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


TEST = f"target/debug/deps/pagination-{'f' * 16}"


def test_cargo_artifacts_select_one_hashed_binary_test_and_symbols(monkeypatch):
    monkeypatch.setattr("peeky.rust.sys.platform", "linux")
    names = [BINARY, BINARY + ".d", TEST, TEST + ".d"]
    assert cargo_artifacts(names, "peeky_pagination", "pagination") == (BINARY, BINARY, TEST)
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    symbols = f"{BINARY}.dSYM/Contents/Resources/DWARF/peeky_pagination-{HASH}"
    names = [
        BINARY,
        BINARY + ".d",
        f"{BINARY}.dSYM/Contents/Info.plist",
        symbols,
        TEST,
        f"{TEST}.dSYM/Contents/Resources/DWARF/pagination-{'f' * 16}",
    ]
    assert cargo_artifacts(names, "peeky_pagination", "pagination") == (BINARY, symbols, TEST)


@pytest.mark.parametrize(
    "platform, names",
    [
        ("linux", [BINARY, BINARY.replace(HASH, "f" * 16), TEST]),
        ("linux", [BINARY, TEST, TEST.replace("f" * 16, "e" * 16)]),
        ("linux", [BINARY]),
        ("linux", [BINARY, TEST, "target/debug/deps/libother.rlib"]),
        ("linux", [BINARY[:-1], TEST]),
        ("linux", [BINARY, TEST, f"{BINARY}.dSYM/Contents/Info.plist"]),
        ("linux", [BINARY.replace("peeky_pagination", "other"), TEST]),
        ("darwin", [BINARY, BINARY + ".d", TEST]),
        ("darwin", [BINARY, TEST, f"{BINARY}.dSYM/Contents/Resources/DWARF/other"]),
    ],
)
def test_cargo_artifacts_reject_missing_duplicate_or_unexpected_outputs(
    monkeypatch, platform, names
):
    monkeypatch.setattr("peeky.rust.sys.platform", platform)
    with pytest.raises(ValueError):
        cargo_artifacts(names, "peeky_pagination", "pagination")
    with pytest.raises(ValueError, match="distinct"):
        cargo_artifacts(names, "pagination", "pagination")


SUMMARY = "test result: {}. {} passed; {} failed; 0 ignored; 0 measured; {} filtered out; finished in 0.14s"


@pytest.mark.parametrize(
    "result, passed",
    [
        ({"exit_code": 0, "stdout": SUMMARY.format("ok", 1, 0, 0)}, True),
        ({"exit_code": 101, "stdout": SUMMARY.format("FAILED", 0, 1, 0)}, False),
        ({"exit_code": 0, "stdout": SUMMARY.format("FAILED", 0, 1, 0)}, False),
        ({"exit_code": 0, "stdout": SUMMARY.format("ok", 0, 0, 0)}, False),
        ({"exit_code": 0, "stdout": SUMMARY.format("ok", 1, 0, 1)}, False),
        ({"exit_code": 0, "stdout": SUMMARY.format("ok", 1, 0, 0) + "\nlater output"}, False),
        (
            {
                "exit_code": 0,
                "stdout": SUMMARY.format("ok", 1, 0, 0).replace("0 ignored", "2 ignored"),
            },
            False,
        ),
        (
            {
                "exit_code": 0,
                "stdout": SUMMARY.format("ok", 1, 0, 0).replace("0 measured", "1 measured"),
            },
            False,
        ),
        ({"exit_code": 0, "stdout": "running 1 test"}, False),
        ({"exit_code": 0, "stdout": SUMMARY.format("ok", 1, 0, 0) * 2}, False),
        ({"exit_code": 0, "stdout": (SUMMARY.format("ok", 1, 0, 0) + "\n") * 2}, False),
        ({"exit_code": None, "timed_out": True, "stdout": SUMMARY.format("ok", 1, 0, 0)}, False),
        (
            {
                "exit_code": 0,
                "output_limit_exceeded": True,
                "stdout": SUMMARY.format("ok", 1, 0, 0),
            },
            False,
        ),
    ],
)
def test_libtest_summary_requires_one_explicit_clean_pass(result, passed):
    assert libtest_summary(result)["passed"] is passed


def bare_session():
    session = object.__new__(RustSession)
    session._layouts_data = json.dumps(LAYOUTS)
    return session


@pytest.mark.parametrize(
    "variable",
    [
        {"name": "some", "type": OPTION, "variablesReference": 0},
        {"name": "byte", "type": "unsigned char", "variablesReference": 0},
        {"name": "flag", "type": "bool", "variablesReference": 0},
        {"name": "wide", "type": "__int128", "variablesReference": 0},
    ],
)
def test_rust_mutation_is_limited_to_integer_locals(variable):
    with pytest.raises(ValueError, match="i16 to i64, u16 to u64, isize, usize"):
        bare_session().validate_variable(variable, "1")
    bare_session().validate_variable({"name": "start", "type": "int"}, "0")
    bare_session().validate_variable(
        {"name": "index", "type": "unsigned long"}, "18446744073709551615"
    )
    with pytest.raises(ValueError, match="outside the range of unsigned int"):
        bare_session().validate_variable({"name": "count", "type": "unsigned int"}, "-1")
    with pytest.raises(ValueError, match="outside the range of long"):
        bare_session().validate_variable({"name": "big", "type": "long"}, "9223372036854775808")


def test_rust_launch_disables_symbol_file_scripts(monkeypatch):
    monkeypatch.setattr(
        NativeSession, "launch_args", lambda self, program, root, args=None: {"args": args}
    )
    assert bare_session().launch_args("p", "r") == {
        "args": None,
        "initCommands": ["settings set target.load-script-from-symbol-file false"],
    }
    # Per-run arguments (--arg) reach the native launch unchanged.
    assert bare_session().launch_args("p", "r", ["2"])["args"] == ["2"]


def test_lldb_waits_for_its_debug_server_as_long_as_peeky_waits_for_lldb(tmp_path):
    # #193: under load, lldb-server answered after LLDB's 5 s packet timeout, and a value
    # it could read came back as `invalid load address` (`p.a` unavailable). LLDB now
    # waits longer than Peeky's own 15 s request bound, which decides.
    from types import SimpleNamespace

    session = object.__new__(RustSession)
    session.assert_current = lambda: None
    session.source_path = lambda program: program
    session.binary = tmp_path / "app"
    session.prepared = SimpleNamespace(workspace=tmp_path)
    session.target = SimpleNamespace(arguments=())
    timeout = "settings set plugin.process.gdb-remote.packet-timeout 30"
    native = NativeSession.launch_args(session, "p", "r")
    assert native["initCommands"] == [timeout], native
    rust = session.launch_args("p", "r")
    assert rust["initCommands"] == [
        timeout,
        "settings set target.load-script-from-symbol-file false",
    ], rust


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

    def request(self, operation, args, timeout=15):
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

    def view_variable(self, variable, fetch, read=None, **_):
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
    assert {view["view"] for view in views[25:]} == {"budget-exhausted"}
    assert UNVERIFIED not in {view["value"] for view in views}
    assert all(view["variablesReference"] == 0 for view in views)
    assert worker.view_fetches == 128
    call(worker, "inspect")
    assert worker.view_fetches == 128


def scripted_toolchain(tmp_path, *, version="1.96.0", cargo_build=""):
    config = toolchain(tmp_path)
    marker = tmp_path / "cargo-calls"
    scripts = {
        "rustc": f"print('rustc {version} (fixture)')",
        "cargo": (
            "import pathlib, sys\n"
            f"if sys.argv[1] == '--version': print('cargo {version} (fixture)'); sys.exit(0)\n"
            f"with open({str(marker)!r}, 'a') as f: f.write(sys.argv[1] + '\\n')\n" + cargo_build
        ),
    }
    for name, body in scripts.items():
        Path(config[name]).write_text(f"#!{sys.executable}\n{body}\n")
    return config, marker


def test_rust_rejects_manifest_changed_between_preflight_and_snapshot(tmp_path, monkeypatch):
    import peeky.rust as rust

    root = example(tmp_path)
    config, marker = scripted_toolchain(tmp_path)
    real = rust.validate_cargo_inputs
    calls = []

    def racing(path, **kwargs):
        result = real(path, **kwargs)
        calls.append(Path(path))
        if len(calls) == 1:
            manifest = Path(path) / "Cargo.toml"
            manifest.write_text(
                manifest.read_text().replace(
                    'name = "peeky-pagination"\npath', 'name = "peeky-other"\npath'
                )
            )
        return result

    monkeypatch.setattr(rust, "validate_cargo_inputs", racing)
    with pytest.raises(RuntimeError, match="Staged Cargo inputs differ"):
        RustTarget(**config).prepare(root / "src/main.rs", root)
    assert len(calls) == 2 and calls[1] != root and not marker.exists()


def test_rust_checks_pinned_versions_before_compiling(tmp_path):
    root = example(tmp_path)
    config, marker = scripted_toolchain(tmp_path, version="1.95.0")
    with pytest.raises(ValueError, match="pinned version"):
        RustTarget(**config).prepare(root / "src/main.rs", root)
    assert not marker.exists()


def test_rust_rechecks_cargo_ancestry_before_each_cargo_command(
    tmp_path, tmp_path_factory, monkeypatch
):
    root = example(tmp_path)
    plant = (
        "parent = pathlib.Path.cwd().parent / '.cargo'\n"
        "parent.mkdir(exist_ok=True)\n"
        "(parent / 'config.toml').write_text('[build]\\nrustc-wrapper = \"/x\"\\n')\n"
    )
    config, marker = scripted_toolchain(tmp_path, cargo_build=plant)
    private = tmp_path_factory.mktemp("rust-tmp")
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    with pytest.raises(ValueError, match="Ambient Cargo configuration"):
        RustTarget(**config).prepare(root / "src/main.rs", root)
    assert marker.read_text() == "build\n"
    assert list(private.glob("peeky-rust-*")) == []


def cargo_message(level, file_name=None, primary=True):
    spans = [] if file_name is None else [{"file_name": file_name, "is_primary": primary}]
    return json.dumps({"reason": "compiler-message", "message": {"level": level, "spans": spans}})


FINISHED = json.dumps({"reason": "build-finished", "success": False})


@pytest.mark.parametrize(
    "lines, retryable",
    [
        ([cargo_message("error", "src/main.rs"), FINISHED], True),
        ([cargo_message("warning", "src/main.rs"), cargo_message("error", "src/main.rs")], True),
        ([cargo_message("warning", "src/main.rs"), cargo_message("error")], False),
        ([cargo_message("error", "src/main.rs"), cargo_message("error")], False),
        ([cargo_message("error", "tests/pagination.rs")], False),
        ([cargo_message("error", "/rustc/abc/library/std/src/lib.rs")], False),
        ([cargo_message("error", "src/main.rs", primary=False)], False),
        ([cargo_message("warning", "src/main.rs"), FINISHED], False),
        ([FINISHED], False),
        (["not json", cargo_message("error", "src/main.rs")], False),
        ([cargo_message("bogus", "src/main.rs")], False),
    ],
)
def test_only_rustc_errors_with_primary_spans_in_declared_sources_are_retryable(lines, retryable):
    if lines and lines[-1] != FINISHED:
        lines = [*lines, FINISHED]
    assert compiler_diagnostic(cargo_failure(lines), ("src/main.rs",)) is retryable


STDERR = (
    "   Compiling peeky-pagination v0.1.0 (/tmp/w)\n"
    'error: could not compile `peeky-pagination` (bin "peeky-pagination") due to 1 previous error\n'
)


def cargo_failure(lines, **changes):
    return {"exit_code": 101, "stdout": "\n".join(lines) + "\n", "stderr": STDERR, **changes}


@pytest.mark.parametrize(
    "changes",
    [
        {"exit_code": None, "output_limit_exceeded": True},
        {"exit_code": -9},
        {"exit_code": 1},
        {"timed_out": True},
        {"stderr": STDERR + "error: failed to run custom build command\n"},
        {"stderr": "   Compiling peeky-pagination v0.1.0 (/tmp/w)\n"},
        {"stdout": cargo_message("error", "src/main.rs") + "\n"},
        # Output that reaches what a build keeps may have been cut short.
        {
            "stdout": (cargo_message("error", "src/main.rs") + "\n")
            * (BUILD_RETAINED // len(cargo_message("error", "src/main.rs")) + 1)
            + FINISHED
            + "\n"
        },
        {"stdout": cargo_message("error", "src/main.rs") + "\n" + FINISHED},
        {
            "stdout": FINISHED
            + "\n"
            + cargo_message("error", "src/main.rs")
            + "\n"
            + FINISHED
            + "\n"
        },
    ],
)
def test_incomplete_or_abnormal_cargo_failures_are_not_retryable(changes):
    good = cargo_failure([cargo_message("error", "src/main.rs"), FINISHED])
    assert compiler_diagnostic(good, ("src/main.rs",)) is True
    assert compiler_diagnostic({**good, **changes}, ("src/main.rs",)) is False


def test_macos_dsymutil_must_be_exactly_the_path_selected_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("peeky.rust.sys.platform", "darwin")
    config = toolchain(tmp_path)
    renamed = tmp_path / "other/symtool"
    renamed.parent.mkdir(exist_ok=True)
    renamed.write_text("#!/bin/sh\n")
    renamed.chmod(0o755)
    with pytest.raises(ValueError, match="named dsymutil"):
        RustTarget(**{**config, "dsymutil": str(renamed)})
    declared = tmp_path / "declared/dsymutil"
    declared.parent.mkdir()
    declared.write_text("#!/bin/sh\n")
    declared.chmod(0o755)
    target = RustTarget(**{**config, "dsymutil": str(declared)})
    env = target._environment(str(Path(config["rustc"]).resolve()))
    assert env["PATH"].split(":")[0] == str(declared.parent)
    declared.chmod(0o644)  # PATH lookup would now select the rustc directory's dsymutil
    with pytest.raises(ValueError, match="PATH does not select"):
        target._environment(str(Path(config["rustc"]).resolve()))


def test_owned_directory_cleanup_failure_is_never_silent(tmp_path, monkeypatch):
    from peeky.rust import _remove_owned

    monkeypatch.setattr("peeky.rust.shutil.rmtree", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="not removed"):
        _remove_owned(tmp_path)
    error = ValueError("original")
    _remove_owned(tmp_path, error)
    assert "not removed" in error.__notes__[0]


def test_launch_deadline_is_passed_to_preparation_and_failure_cleans(tmp_path):
    class SlowTarget(PythonAdapter):
        supports_deadline = True
        remaining = []

        def prepare(self, program, root, *, deadline):
            self.remaining.append(deadline - time.monotonic())
            raise TimeoutError("Rust preparation exceeded its overall deadline")

    (tmp_path / "main.rs").write_text("fn main() {}\n")
    worker = DebugWorker(tmp_path, adapter=SlowTarget())
    with pytest.raises(TimeoutError):
        worker.dispatch("launch", {"program": "main.rs"})
    assert 0 < SlowTarget.remaining[0] <= worker.launch_deadline_s < 45
    assert worker.session is None and worker.runtime is None and worker.dap is None


def test_launch_finishing_after_deadline_is_discarded(tmp_path, monkeypatch):
    closed = []

    class Target(PythonAdapter):
        def close(self):
            closed.append("runtime")

    class SlowDAP:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, command, arguments=None, **kwargs):
            if command == "configurationDone":
                time.sleep(0.3)
            return {"breakpoints": []}

        def send(self, command, arguments=None, **kwargs):
            return 1

        def wait_event(self, *args, **kwargs):
            return {}

        def response(self, *args, **kwargs):
            return {}

        def close(self):
            closed.append("dap")

    monkeypatch.setattr("peeky.worker.DAP", SlowDAP)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Target())
    worker.launch_deadline_s = 0.1
    with pytest.raises(TimeoutError, match="discarded"):
        worker.dispatch("launch", {"program": "target.py"})
    assert closed == ["dap", "runtime"] and worker.session is None


class MutableRuntime(Runtime):
    def validate_value(self, value):
        pass

    def validate_variable(self, variable, value):
        assert variable["type"] == "int"


def test_value_views_rebind_mutation_to_freshly_reacquired_scope(worker):
    worker.runtime = MutableRuntime()
    backend = worker.dap
    scopes = iter([7, 70])
    original = backend.request

    def request(operation, args, timeout=15):
        if operation == "scopes":
            return {
                "scopes": [
                    {
                        "name": "Locals",
                        "presentationHint": "locals",
                        "variablesReference": next(scopes),
                    }
                ]
            }
        if operation == "setVariable":
            backend.calls.append((operation, args))
            return {"value": args["value"]}
        if operation == "variables" and args["variablesReference"] == 70:
            return {"variables": deepcopy(backend.roots)}
        return original(operation, args)

    backend.request = request
    scope = call(worker, "inspect")["scopes"][0]
    assert worker.view_fetches and worker.reference_adapter[scope["reference"]] == 70
    call(worker, "set_variable", reference=scope["reference"], name="start", value="0")
    assert backend.calls[-1] == (
        "setVariable",
        {"variablesReference": 70, "name": "start", "value": "0"},
    )


def test_value_views_reject_changed_scope_identity(worker):
    backend = worker.dap
    names = iter(["Locals", "Other"])
    original = backend.request

    def request(operation, args, timeout=15):
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": next(names), "presentationHint": "locals", "variablesReference": 7}
                ]
            }
        return original(operation, args)

    backend.request = request
    with pytest.raises(ValueError, match="Scopes changed"):
        call(worker, "inspect")
    assert not worker.references


def test_synthetic_rust_fields_are_refused_at_every_level(worker):
    worker.dap.roots.append(raw("$discr$", "unsigned int", 0, "1"))
    with pytest.raises(ValueError, match="Raw Rust enum storage"):
        call(worker, "inspect")
    worker.dap.roots.pop()
    worker.dap.children[31] = [raw("$variant$0", "app::Unknown$Inner::A$Variant", 33)]
    variables = {v["name"]: v for v in call(worker, "inspect")["scopes"][0]["variables"]}
    holder = call(worker, "expand", handle=variables["holder"]["variablesReference"])
    with pytest.raises(ValueError, match="Raw Rust enum storage"):
        call(worker, "expand", handle=holder["variables"][0]["variablesReference"])


def test_value_view_time_budget_degrades_to_opaque(worker, monkeypatch):
    monkeypatch.setattr("peeky.worker.VIEW_SECONDS", 0)
    variables = {v["name"]: v for v in call(worker, "inspect")["scopes"][0]["variables"]}
    assert variables["some"]["view"] == "budget-exhausted" and worker.view_fetches == 0


def test_a_spent_value_view_budget_says_which_limit_ran_out(worker, monkeypatch):
    # Review of #156, round 3: Peeky's own checks took the whole time, and a `&str` said
    # "<Rust enum view budget exhausted>"; the row names the limit, and how much of the
    # time the debugger took.
    worker.dap.roots = [raw(f"some{index}", OPTION, 10) for index in range(30)]
    views = call(worker, "inspect")["scopes"][0]["variables"]
    assert {view["value"] for view in views[25:]} == {
        "<Rust value view budget exhausted (128 debugger reads); expand or inspect a "
        "narrower scope>"
    }
    monkeypatch.setattr("peeky.worker.VIEW_SECONDS", 0)
    worker.dap.roots = [raw("some", OPTION, 10)]
    [view] = call(worker, "inspect")["scopes"][0]["variables"]
    assert view["value"] == (
        "<Rust value view budget exhausted (its 0 s ran out, 0.0 s of them waiting for the "
        "debugger's reads, the rest listing values and in Peeky's own checks); expand or "
        "inspect a narrower scope>"
    )


def test_each_expansion_gets_a_fresh_value_view_budget(worker):
    worker.dap.roots = [raw("holder", "app::Holder", 30)] + [
        raw(f"some{index}", OPTION, 10) for index in range(30)
    ]
    worker.dap.children[30] = [raw("inner", OPTION, 10)]
    variables = call(worker, "inspect")["scopes"][0]["variables"]
    assert variables[-1]["view"] == "budget-exhausted"
    expanded = call(worker, "expand", handle=variables[0]["variablesReference"])
    assert expanded["variables"][0]["value"] == "Some(3)" and worker.view_fetches == 5


def test_launch_deadline_bounds_every_dap_phase(tmp_path, monkeypatch):
    closed, timeouts = [], []

    class Target(PythonAdapter):
        def close(self):
            closed.append("runtime")

    class SlowDAP:
        def __init__(self, *args, **kwargs):
            pass

        def _slow(self, timeout):
            timeouts.append(timeout)
            time.sleep(min(0.05, timeout))

        def request(self, command, arguments=None, *, timeout=15):
            self._slow(timeout)
            return {"breakpoints": []}

        def send(self, command, arguments=None, *, timeout=5):
            self._slow(timeout)
            return 1

        def wait_event(self, names, after=0, timeout=15, **kwargs):
            self._slow(timeout)
            return {}

        def response(self, seq, timeout=15):
            self._slow(timeout)
            return {}

        def close(self):
            closed.append("dap")

    monkeypatch.setattr("peeky.worker.DAP", SlowDAP)
    (tmp_path / "target.py").write_text("pass\n")
    worker = DebugWorker(tmp_path, adapter=Target())
    worker.launch_deadline_s = 0.12
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        worker.dispatch("launch", {"program": "target.py"})
    assert time.monotonic() - started < 0.3
    assert all(timeout <= 0.12 for timeout in timeouts) and len(timeouts) < 6
    assert closed == ["dap", "runtime"] and worker.session is None


def test_native_and_java_targets_receive_the_launch_deadline():
    from peeky.java import JavaTarget
    from peeky.native import NativeTarget

    assert NativeTarget.supports_deadline and JavaTarget.supports_deadline
    assert RustTarget.supports_deadline


def test_view_response_timeout_is_budget_not_session_failure(worker):
    from peeky.dap import ResponseTimeout

    original = worker.dap.request

    def request(operation, args, timeout=15):
        if operation == "variables" and args["variablesReference"] == 11:
            raise ResponseTimeout("late")
        return original(operation, args)

    worker.dap.request = request
    variables = {v["name"]: v for v in call(worker, "inspect")["scopes"][0]["variables"]}
    assert variables["some"]["view"] == "budget-exhausted"
    assert worker.dap is not None and worker.session == "session"


def test_value_views_reject_duplicate_scope_identities(worker):
    backend = worker.dap
    orders = iter([[7, 8], [8, 7]])
    original = backend.request
    backend.children[8] = [raw("start", "int", 0, "9")]

    def request(operation, args, timeout=15):
        if operation == "scopes":
            return {
                "scopes": [
                    {"name": "Locals", "presentationHint": "locals", "variablesReference": ref}
                    for ref in next(orders)
                ]
            }
        if operation == "variables" and args["variablesReference"] == 8:
            return {"variables": deepcopy(backend.children[8])}
        return original(operation, args)

    backend.request = request
    with pytest.raises(ValueError, match="Duplicate scope"):
        call(worker, "inspect")
    assert not worker.references


def test_pending_view_reply_blocks_mutation_until_drained(worker):
    from peeky.dap import ResponseTimeout

    backend = worker.dap
    original = backend.request
    state = {"timeout": True, "drained": False}

    def request(operation, args, timeout=15):
        if operation == "variables" and args["variablesReference"] == 11 and state["timeout"]:
            raise ResponseTimeout("late")
        return original(operation, args)

    backend.request = request
    backend.drained = lambda timeout=1: state["drained"]
    snapshot = call(worker, "inspect")
    assert snapshot["mutation_blocked"] and not worker.references
    state.update(timeout=False)
    assert call(worker, "inspect").get("mutation_blocked") and not worker.references
    state.update(drained=True)
    snapshot = call(worker, "inspect")
    assert "mutation_blocked" not in snapshot and worker.references


def test_provenance_is_rechecked_on_wait_and_report(worker):
    worker.state = "running"
    worker.cursor = 0
    worker.dap.wait_event = lambda names, cursor, timeout: {
        "event": "terminated",
        "cursor": 1,
        "body": {},
    }

    def changed():
        raise RuntimeError("Attached sources, classes or adapter changed; re-attach")

    worker.runtime.assert_current = changed
    with pytest.raises(RuntimeError, match="changed"):
        call(worker, "wait")
    assert worker.dap is None and worker.session is None


def test_rust_locals_are_settable_only_when_kept_in_memory():
    session = bare_session()
    session.memory = {("h0123456789abcdef", "index"): True, ("h0123456789abcdef", "copy"): False}
    frame = {"name": "demo::f::h0123456789abcdef"}
    assert session.settable({"name": "index", "type": "unsigned long"}, frame) is True
    assert session.settable({"name": "copy", "type": "int"}, frame) is False
    assert session.settable({"name": "index", "type": "unsigned char"}, frame) is False
    assert session.settable({"name": "index", "type": "unsigned long"}, {"name": "f"}) is False


def test_a_set_is_refused_before_launch_only_where_every_function_on_the_line_fails(
    tmp_path,
):
    # #138: a closure, a macro's twins or an inlined copy on the line may keep the local
    # in memory: refused only when every function that can stop there rules it out.
    import json
    from types import SimpleNamespace

    from peeky.rust import RustSession
    from peeky.rust_mir import IrFunction

    def function(name, lines=(), copies=(), ancestors=("app",)):
        return IrFunction(
            name,
            ancestors[-1],
            "src/main.rs",
            {"start": set(lines)},
            {},
            copies=frozenset(copies),
            ancestors=ancestors,
        )

    def body(bindings, arguments=(), types=None, saved=(), coroutine=None):
        return SimpleNamespace(
            bindings={name: [(local, "src/main.rs", 3, 1, 0)] for name, local in bindings.items()},
            arguments=set(arguments),
            types=types or {},
            visible_scopes=lambda line, path: {0},
            coroutine=coroutine,
            saved=frozenset(saved),
            places={},
        )

    functions = {
        "h1": function("outer", [5]),
        "h2": function("{closure#0}", [5], ancestors=("app", "outer")),
        "h3": function("inlined", [], copies=[9]),
        "h4": function("elsewhere", [7]),
        "h5": function("{async_fn#0}", [11], ancestors=("app", "aw")),
    }
    bodies = {
        "h1": body({"width": 1, "total": 2, "flag": 3}, [1], {1: "u64", 2: "u64", 3: "bool"}),
        "h2": body({"v": 1}, [1], {1: "&u64"}),
        "h3": body({"x": 1}, [1], {1: "u32"}),
        "h5": body({"k": 37, "q": 27}, [1], {37: "i32", 27: "i32"}, saved=[37], coroutine=36),
    }
    session = object.__new__(RustSession)
    session.target = SimpleNamespace(opt_level="0")
    session.prepared = SimpleNamespace(source_root=tmp_path)
    session._lines_data = json.dumps({"src/main.rs": [5, 7, 9, 11]})
    session.memory = {("h1", "total"): True, ("h1", "flag"): True, ("h1", "width"): False}
    session.memory[("h3", "x")] = False
    session.memory.update({("h5", "k"): False, ("h5", "q"): True})
    # What the IR declares each on: a `.dbg.spill` copy (review of PR #335: no declaration
    # proves no register).
    session.declarations_ir = {("h1", "width"): [(3, "copy")], ("h3", "x"): [(3, "copy")]}
    session.owned = SimpleNamespace(
        functions=functions,
        body=bodies.get,
        declared_type=lambda hashed, name, line, path: bodies[hashed].types.get(
            bodies[hashed].bindings[name][0][0]
        ),
    )
    program = tmp_path / "src/main.rs"

    def judged(name, line=5):
        return session.never_settable(program, [line], [name])

    message = judged("width")
    assert "in app::outer, `width` is a parameter kept in a register" in message, message
    assert "app::outer::{closure#0} has no local `width`" in message, message
    assert "`mut width` in the signature" in message and message.endswith(
        "settable here: total (root locals; fields too where rows mark them [settable])"
    )
    assert judged("total") is None
    assert "`flag` is a bool; only integer locals" in judged("flag")
    code, message = judged("nope")
    # One reason for both functions, said once (#151 review 2).
    assert (
        code == "NOT_FOUND"
        and "app::outer and app::outer::{closure#0} have no local `nope`" in (message)
    )
    # A function only inlined on the line (its `copies`) is judged there too.
    assert "`x` is a parameter kept in a register" in judged("x", 9)
    # A line the debugger moves its breakpoint from, or no function's: left to the stop.
    assert judged("width", 6) is None and judged("width", 8) is None
    # Kept in an async body's state across an `.await`: no `black_box` helps there.
    message = judged("k", 11)
    assert "`k` is held in the async body's state across an `.await`" in message, message
    assert "black_box(&mut k)" not in message and message.endswith(
        "settable here: q (root locals; fields too where rows mark them [settable])"
    )
    session.owned.precise = False
    assert judged("width") is None
    session.owned.precise = True
    session.memory[("h1", "width")] = True
    assert judged("width") is None
    session.target.opt_level = "1"
    assert session.never_settable_row({"name": "x", "type": "int"}, {}) is None


def test_a_row_says_register_only_where_every_piece_is_the_debuggers_copy():
    # Review of PR #335: a struct's row, and its fields', said `[not settable: register]`
    # wherever the IR's memory proof wasn't True: a closure's capture (declared through its
    # environment's pointer), a binding with no declaration, pieces in a slot and in a
    # register. Only a binding whose every declaration is a `.dbg.spill` copy is in a
    # register; anything else says only what is true: it isn't proven in memory.
    from types import SimpleNamespace

    from peeky.rust import RustSession

    hashed = "h0123456789abcdef"
    session = object.__new__(RustSession)
    session.target = SimpleNamespace(opt_level="0")
    body = SimpleNamespace(bindings={}, coroutine=None)
    session.owned = SimpleNamespace(
        body=lambda hashed: body, _local=lambda body, name, line, path: None
    )
    session.declarations_ir = {
        (hashed, "pair"): [(3, "copy")],
        (hashed, "split"): [(4, "copy"), (4, "slot")],
        (hashed, "lap"): [(14, "unknown")],
        (hashed, "kept"): [(5, "slot")],
    }
    session.memory = {(hashed, name): False for name in ("pair", "split", "lap", "nothing")}
    session.memory[(hashed, "kept")] = True
    frame = {"name": f"app::f::{hashed}", "line": 20, "source": {"path": "/w/src/main.rs"}}

    def row(name):
        return session.never_settable_row({"name": name, "type": "app::P"}, frame, member=True)

    assert row("pair") == "register"
    assert row("kept") is None
    for name in ("split", "lap", "nothing"):
        assert row(name) == "not proven in memory", name
    # A root integer row says the same.
    assert session.never_settable_row({"name": "lap", "type": "unsigned int"}, frame) == (
        "not proven in memory"
    )


def test_a_name_not_proven_in_memory_says_which_binding_or_copy_is_why():
    # Review of #138: the IR's proof covers every binding of a name; the binding asked
    # for may have its slot while another, or an inlined copy, does not.
    from types import SimpleNamespace

    from peeky.rust import RustSession

    bindings = {"x": [(1, "src/main.rs", 4, None, 0), (2, "src/main.rs", 6, None, 0)]}
    bindings["y"] = [(3, "src/main.rs", 9, None, 0)]
    body = SimpleNamespace(bindings=bindings)
    session = object.__new__(RustSession)
    session.owned = SimpleNamespace(
        body=lambda hashed: body, _local=lambda body, name, line, path: {8: 2}.get(line)
    )
    session.declarations_ir = {
        ("h", "x"): [(4, "copy"), (6, "slot")],
        ("h", "y"): [(9, "copy")],
        ("h", "far"): [(3, "inlined"), (3, "inlined")],
        ("h", "both"): [(5, "copy"), (5, "slot")],
        # Review of PR #335: pieces of one binding, one spilled and one in a slot; a
        # closure's capture, declared through its environment's pointer.
        ("h", "piece"): [(7, "copy"), (7, "copy"), (7, "slot")],
        ("h", "capture"): [(14, "unknown")],
        ("h", "spilled"): [(15, "copy"), (15, "copy")],
    }
    judged = session._not_in_memory
    assert judged("h", "x @ main.rs:6") == ("other", [4])
    assert judged("h", "x @ main.rs:4") == ("own", None)
    assert judged("h", "x", 8, "src/main.rs") == ("other", [4])  # the line's binding
    assert judged("h", "x", 7, "src/main.rs") == ("some", [4])  # not told which
    assert judged("h", "y") == ("own", None)
    assert judged("h", "far") == ("inlined", None)
    # Review of PR #335: "register" only where every piece of the binding is a spill
    # copy; a slot and a spill on its line, no declaration, or one through a pointer
    # prove nothing.
    assert judged("h", "both") == ("unknown", None)
    assert judged("h", "unknown") == ("unknown", None)
    assert judged("h", "piece") == ("unknown", None)
    assert judged("h", "capture") == ("unknown", None)
    assert judged("h", "spilled") == ("own", None)
    rule, advice = RustSession._register_text("app::f", "lap", ("unknown", None))
    assert "doesn't prove `lap` in memory" in rule and "register" not in rule.split(":")[0]
    assert "a change might not reach the program" in rule and "candidate edit" in advice
    rule, advice = RustSession._register_text("app::f", "x", ("other", [4]))
    assert "another binding of `x` (declared on line 4)" in rule
    assert "after the `let` on line 4" in advice
    rule, advice = RustSession._register_text("app::far", "y", ("inlined", None))
    assert "inlined where it is called" in rule and advice.startswith("Inlining prevents")
    # `describe` gives each name the reason its rows give (review round 3).
    session.memory = {
        ("h0123456789abcdef", name): False
        for name in ("x", "y", "far", "k", "both", "piece", "capture", "spilled")
    }
    session.declarations_ir = {
        ("h0123456789abcdef", name): found for (_, name), found in session.declarations_ir.items()
    }
    # `k`: a local an async body keeps in its state across an `.await`.
    body.bindings["k"] = [(5, "src/main.rs", 12, None, 0)]
    body.saved, body.coroutine, body.places = {5}, 1, {}
    session.owned = SimpleNamespace(body=lambda hashed: body)
    linkages = {"_ZN3app1f17h0123456789abcdefE"}
    names = {"x": {"u64"}, "y": {"u32"}, "far": {"u64"}, "k": {"u64"}, "s": {"String"}}
    names.update({name: {"u32"} for name in ("both", "piece", "capture", "spilled")})
    assert session._register_names(linkages, names) == {
        "x": "another binding in a register (line 4)",
        "y": "register",
        "far": "inlined",
        "k": "async state",
        "both": "not proven in memory",
        "piece": "not proven in memory",
        "capture": "not proven in memory",
        "spilled": "register",
    }


@pytest.mark.parametrize(
    "texts,found",
    [
        ({"src/main.rs": "fn main() { let v = vec![1]; }"}, None),
        ({"src/main.rs": "fn main() { unsafe { f() } }"}, "src/main.rs"),
        ({"src/main.rs": "fn main() {}", "src/m.rs": "unsafe fn g() {}"}, "src/m.rs"),
        ({"src/main.rs": "#[unsafe(no_mangle)] pub fn f() {}"}, "src/main.rs"),
        ({"src/main.rs": "#[no_mangle] pub fn free() {}"}, "src/main.rs"),
        ({"src/main.rs": 'unsafe extern "C" { fn getpid() -> i32; }'}, "src/main.rs"),
        ({"src/main.rs": "macro_rules! m { () => { unsafe { 1 } }; }"}, "src/main.rs"),
        # Review of #156, round 3: `global_asm!` needs no `unsafe`, and can define `free`.
        ({"src/main.rs": 'std::arch::global_asm!(".globl free");'}, "src/main.rs"),
        ({"src/main.rs": "use std::arch::global_asm as g;"}, "src/main.rs"),
        ({"src/main.rs": 'fn f() { asm!("nop") }'}, "src/main.rs"),
        # Review of #156, round 4: a macro may drop the `#` of `#unsafe`; only
        # `r#unsafe` is an identifier (`r#no_mangle` is `no_mangle`).
        (
            {
                "src/main.rs": "macro_rules! run { (# $($t:tt)*) => { $($t)* }; }\n"
                "fn main() { run!(#unsafe { f() }); }"
            },
            "src/main.rs",
        ),
        ({"src/main.rs": "#[r#no_mangle] fn free() {}"}, "src/main.rs"),
        ({"src/main.rs": "r#unsafe(); fn r#unsafe() {}"}, None),
        # Not code: a comment, a string, a raw identifier, a lint's name.
        ({"src/main.rs": '// unsafe\nfn f() -> &\'static str { "unsafe" }'}, None),
        ({"src/main.rs": "fn r#unsafe() {}\n#![forbid(unsafe_code)]"}, None),
        ({"src/main.rs": "fn asm() {} fn naked() {}"}, None),
        # A shebang is no code (rustc skips it), so its `"` opens no string.
        (
            {"src/main.rs": '#!/usr/bin/env -S echo "\nfn f() { unsafe { g() } } // "'},
            "src/main.rs",
        ),
        ({"src/main.rs": '#!/bin/run "\nfn f() {}'}, None),
        # Any other file the build reads is read as text.
        ({"src/main.rs": "fn main() {}", "inc.txt": "unsafe { x() }"}, "inc.txt"),
    ],
)
def test_the_crates_unsafe_code_is_found_in_any_of_its_files(texts, found):
    # Review of #156: unsafe code anywhere in the crate (a closure, a callee, a pointer
    # stored for later) can free what a Vec field still points at; dependencies (std)
    # are taken as sound.
    from peeky.rust import unsafe_source

    rust = tuple(name for name in texts if name.endswith(".rs"))
    why = unsafe_source(texts, rust)
    if found is None:
        assert why is None
    else:
        assert why == (
            f"the crate has unsafe code (in {found}), which can free a buffer without Peeky "
            "seeing it"
        )


@pytest.mark.parametrize(
    "texts,found",
    [
        ({"src/main.rs": "#![feature(custom_mir, core_intrinsics)]\nfn main() {}"}, "src/main.rs"),
        ({"src/main.rs": "#![cfg_attr(all(), feature(custom_mir))]"}, "src/main.rs"),
        ({"src/main.rs": "# /* c */ ! [ feature (x) ]"}, "src/main.rs"),
        ({"src/main.rs": "fn main() { #![allow(x)] #![feature(y)] }"}, "src/main.rs"),
        ({"src/main.rs": "fn main() {}", "src/m.txt": "#![feature(x)]"}, "src/m.txt"),
        # Not a feature: a Cargo feature's `cfg`, a function of that name, a string.
        ({"src/main.rs": '#[cfg(feature = "x")]\nfn f() {}'}, None),
        ({"src/main.rs": "#![allow(dead_code)]\nfn feature(x: i32) {}"}, None),
        ({"src/main.rs": '#![doc = "feature(x)"]'}, None),
    ],
)
def test_a_nightly_feature_counts_as_unsafe_code(texts, found):
    # Review of #156, round 4: Peeky's MIR builds set RUSTC_BOOTSTRAP, so
    # `#![feature(custom_mir)]` works, whose `Drop(*v, ...)` frees what a `&Vec` points
    # at with no `unsafe` token.
    from peeky.rust import unsafe_source

    rust = tuple(name for name in texts if name.endswith(".rs"))
    why = unsafe_source(texts, rust)
    if found is None:
        assert why is None
    else:
        assert why == (
            f"the crate enables nightly features (in {found}), which can free a buffer "
            "without Peeky seeing it"
        )


@pytest.mark.parametrize(
    "text,why",
    [
        ('fn f() { "unterminated }', "a comment, string or char literal does not end"),
        # Review of #156, round 3: frontmatter is not read; a comment after `#!` is not
        # judged; brackets that do not pair up show a literal misread.
        (
            '---\nx = "\n---\nfn f() { unsafe { g() } } // "',
            "it opens with a `---` frontmatter block",
        ),
        ("\n  ---cargo\n---\nfn main() {}", "it opens with a `---` frontmatter block"),
        ("#! // c\n[allow(x)] fn f() {}", "a comment follows its `#!`"),
        ("fn f() { (1 }", "its brackets do not pair up"),
        ("fn f() { 1 ", "its brackets do not pair up"),
    ],
)
def test_a_source_peeky_cannot_be_sure_it_reads_counts_as_unsafe(text, why):
    from peeky.rust import unsafe_source

    found = unsafe_source({"src/main.rs": text}, ("src/main.rs",))
    assert found.startswith("Peeky cannot rule out unsafe code in src/main.rs (" + why), found
    assert found.endswith("and unsafe code can free a buffer without Peeky seeing it")


TRY_MIR = """// MIR for `parse` after PreCodegen

fn parse(_1: &str) -> Result<i64, ParseIntError> {
    debug text => _1;                    // in scope 0 at src/main.rs:1:10: 1:14
    let mut _0: std::result::Result<i64, std::num::ParseIntError>; // return place in scope 0 at src/main.rs:1:25: 1:61
    let _2: i64;                         // in scope 0 at src/main.rs:2:9: 2:14
    let _9: std::result::Result<std::convert::Infallible, std::num::ParseIntError>; // in scope 0 at src/main.rs:2:41: 2:42
    let _12: i64;                        // in scope 0 at src/main.rs:2:22: 2:42
    scope 1 {
        debug cents => _2;               // in scope 1 at src/main.rs:2:9: 2:14
        let _13: i64;                    // in scope 1 at src/main.rs:3:9: 3:12
        scope 6 {
            debug val => _13;            // in scope 6 at src/main.rs:3:9: 3:12
        }
    }
    scope 2 {
        debug residual => _9;            // in scope 2 at src/main.rs:2:41: 2:42
        scope 3 {
        }
    }
    scope 4 {
        debug val => _12;                // in scope 4 at src/main.rs:2:22: 2:42
        scope 5 {
        }
    }

    bb0: {
        return;                          // scope 0 at src/main.rs:5:2: 5:2
    }
}
"""


TRY_SOURCE = """fn parse(text: &str) -> Result<i64, std::num::ParseIntError> {
    let cents: i64 = text.trim().parse()?;
    let val = cents + 1;
    Ok(val)
}
"""


def _try_bindings(tmp_path, source=TRY_SOURCE, mir=TRY_MIR):
    from peeky.rust import RustSession
    from peeky.rust_mir import parse_body

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/main.rs").write_text(source)
    body = parse_body(mir)
    session = SimpleNamespace(
        lines={"src/main.rs": (1, 2, 3, 4, 5)},
        prepared=SimpleNamespace(source_root=tmp_path),
    )
    texts = {}
    return lambda name, line: RustSession._try_bindings(
        session, [body], "src/main.rs", name, line, texts
    )


def test_a_questions_marks_bindings_are_told_by_where_their_spans_end(tmp_path):
    # prep 7: `describe` listed `residual @ main.rs:47` and `val @ main.rs:47` beside the
    # program's own names. The DWARF doesn't mark them artificial; the MIR's span of each
    # ends right after the `?` (`residual` spans only it, `val` the expression it tries).
    desugared = _try_bindings(tmp_path)
    assert desugared("residual", 2) and desugared("val", 2)
    # The program's own `val`, and its other names, end with their names: listed.
    assert not desugared("val", 3) and not desugared("cents", 2) and not desugared("text", 1)


def test_a_binding_is_listed_where_its_source_says_otherwise(tmp_path):
    # The same spans over a source whose line 2 has no `?` there (a stale or other file).
    changed = TRY_SOURCE.replace("parse()?;", "parse().unwrap();")
    assert not _try_bindings(tmp_path, source=changed)("residual", 2)
    # A binding whose span the MIR doesn't give is listed.
    bare = TRY_MIR.replace(
        "// in scope 2 at src/main.rs:2:41: 2:42", "// in scope 2 at src/main.rs:2:41"
    )
    assert not _try_bindings(tmp_path, mir=bare)("residual", 2)


LOOP_MIR = """// MIR for `plan` after PreCodegen

fn plan(_1: &mut [Engineer], _2: u32) -> u32 {
    debug team => _1;                    // in scope 0 at src/main.rs:1:9: 1:13
    debug weeks => _2;                   // in scope 0 at src/main.rs:1:32: 1:37
    let mut _0: u32;                     // return place in scope 0 at src/main.rs:1:45: 1:48
    let mut _3: std::ops::RangeInclusive<u32>; // in scope 0 at src/main.rs:3:17: 3:26
    let mut _5: u32;                     // in scope 0 at src/main.rs:3:9: 3:13
    let mut _6: std::iter::Enumerate<std::slice::Iter<'_, Engineer>>; // in scope 0 at src/main.rs:4:23: 4:46
    let _7: usize;                       // in scope 0 at src/main.rs:4:14: 4:15
    let _8: &Engineer;                   // in scope 0 at src/main.rs:4:17: 4:18
    let _9: usize;                       // in scope 0 at src/main.rs:8:9: 8:13
    scope 1 {
        debug iter => _3;                // in scope 1 at src/main.rs:3:17: 3:26
        scope 2 {
            debug week => _5;            // in scope 2 at src/main.rs:3:9: 3:13
            scope 3 {
                debug iter => _6;        // in scope 3 at src/main.rs:4:23: 4:46
                scope 4 {
                    debug i => _7;       // in scope 4 at src/main.rs:4:14: 4:15
                    debug e => _8;       // in scope 4 at src/main.rs:4:17: 4:18
                }
            }
        }
    }
    scope 5 {
        debug iter => _9;                // in scope 5 at src/main.rs:8:9: 8:13
    }

    bb0: {
        return;                          // scope 0 at src/main.rs:10:2: 10:2
    }
}
"""


LOOP_SOURCE = """fn plan(team: &mut [Engineer], weeks: u32) -> u32 {
    let mut n = 0;
    for week in 1..=weeks {
        for (i, e) in team.iter().enumerate() {
            n += week + e.shifts + i as u32;
        }
    }
    let iter = n;
    iter
}
"""


def _loop_iterators(tmp_path, source=LOOP_SOURCE, mir=LOOP_MIR):
    from peeky.rust import RustSession
    from peeky.rust_mir import parse_body

    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/main.rs").write_text(source)
    body = parse_body(mir)
    session = SimpleNamespace(
        lines={"src/main.rs": tuple(range(1, 11))},
        prepared=SimpleNamespace(source_root=tmp_path),
    )
    texts = {}
    return lambda name, line: RustSession._loop_iterators(
        session, [body], "src/main.rs", name, line, texts
    )


def test_a_for_loops_iterator_is_told_by_the_block_after_its_span(tmp_path):
    # Prep 7: `describe` and `session inspect` listed `iter @ main.rs:32` and
    # `iter @ main.rs:34`, the `for` loops' iterators. The MIR's span of each is the loop's
    # iterator expression, which the loop's `{` follows.
    desugared = _loop_iterators(tmp_path)
    assert desugared("iter", 3) and desugared("iter", 4)
    # The program's own `iter` spans its name: listed. So are the loops' own bindings.
    assert not desugared("iter", 8) and not desugared("week", 3) and not desugared("e", 4)


def test_an_iterator_is_listed_where_its_source_says_otherwise(tmp_path):
    # A loop whose block opens on the next line still counts.
    split = LOOP_SOURCE.replace("for week in 1..=weeks {", "for week in 1..=weeks\n    {").replace(
        "    let iter = n;", "    let iter = n; // moved"
    )
    split_mir = LOOP_MIR.replace("src/main.rs:8:", "src/main.rs:9:").replace(
        "src/main.rs:4:", "src/main.rs:5:"
    )
    assert _loop_iterators(tmp_path, split, split_mir)("iter", 3)
    # A source whose line 3 holds no loop there (a stale or other file): listed.
    changed = LOOP_SOURCE.replace("for week in 1..=weeks {", "let week = 1..=weeks; weeks;")
    assert not _loop_iterators(tmp_path, changed)("iter", 3)
    # A binding whose span the MIR doesn't give is listed.
    bare = LOOP_MIR.replace(
        "// in scope 1 at src/main.rs:3:17: 3:26", "// in scope 1 at src/main.rs:3:17"
    )
    assert not _loop_iterators(tmp_path, LOOP_SOURCE, bare)("iter", 3)
