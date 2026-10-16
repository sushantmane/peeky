"""Rust target/session contracts and worker value-view hooks without a real toolchain."""

import json
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

import pytest

from peeky.adapters import PythonAdapter
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
    monkeypatch.setattr(
        NativeSession, "launch_args", lambda self, program, root, args=None: {"args": args}
    )
    assert bare_session().launch_args("p", "r") == {
        "args": None,
        "initCommands": ["settings set target.load-script-from-symbol-file false"],
    }
    # Per-run arguments (--arg) reach the native launch unchanged.
    assert bare_session().launch_args("p", "r", ["2"])["args"] == ["2"]


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
        {"stdout": (cargo_message("error", "src/main.rs") + "\n") * 300 + FINISHED + "\n"},
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
