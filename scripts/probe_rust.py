#!/usr/bin/env python3
"""Engineering gate only: pinned Rust + raw LLDB DAP, not shipped Rust support."""

import argparse
import hashlib
import json
import os
import platform
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from peeky.agent import execute
from peeky.dap import DAP
from peeky.native import NativeSession, _metadata, elf_build_id
from peeky.rust_manifest import assert_cargo_ancestry_clear, validate_cargo_inputs

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/pagination.rs")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command, cwd, env, timeout=30):
    result = execute(command, cwd, timeout, env=env)
    if result["exit_code"] != 0 or result.get("timed_out"):
        raise RuntimeError(f"Probe command failed: {command!r}: {result!r}")
    return result


def probe(args):
    result = {
        "qualification_only": True,
        "platform": platform.platform(),
        "formatter_policy": "raw; no Rust formatter imported",
        "rust_version_pin": "1.96.0",
    }
    started = time.monotonic()
    paths = {
        name: Path(getattr(args, name)).resolve(strict=True)
        for name in ("cargo", "rustc", "adapter", "dwarfdump", "linker")
    }
    if args.dsymutil:
        paths["dsymutil"] = Path(args.dsymutil).resolve(strict=True)
    result["tools"] = {
        name: {"path": str(path), "sha256": sha(path)} for name, path in paths.items()
    }
    validate_cargo_inputs(
        ROOT / "examples/rust", sources=("src/main.rs",), tests=("tests/pagination.rs",)
    )
    original = {name: sha(ROOT / "examples/rust" / name) for name in INPUTS}
    result["original_inputs"] = original
    try:
        with tempfile.TemporaryDirectory(prefix="peeky-rust-gate-") as temporary:
            root = Path(temporary).resolve()
            assert_cargo_ancestry_clear(root)
            for name in INPUTS:
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((ROOT / "examples/rust" / name).read_bytes())
            home = root / "home"
            home.mkdir()
            env = {
                "PATH": f"{paths['rustc'].parent}:/usr/bin:/bin",
                "HOME": str(home),
                "CARGO_HOME": str(home / "cargo"),
                "CARGO_TARGET_DIR": str(root / "target"),
                "RUSTC": str(paths["rustc"]),
                "CARGO_INCREMENTAL": "0",
                "CARGO_PROFILE_DEV_OPT_LEVEL": "0",
                "CARGO_PROFILE_DEV_DEBUG": "2",
                "CARGO_PROFILE_DEV_SPLIT_DEBUGINFO": "packed"
                if platform.system() == "Darwin"
                else "off",
                "CARGO_PROFILE_DEV_STRIP": "none",
                "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(
                    ["-C", "dwarf-version=4", "-C", f"linker={paths['linker']}"]
                ),
                "LC_ALL": "C",
            }
            if args.sysroot:
                env["SDKROOT"] = str(Path(args.sysroot).resolve(strict=True))
            result["rustc_version"] = run([str(paths["rustc"]), "-Vv"], root, env)["stdout"]
            result["cargo_version"] = run([str(paths["cargo"]), "--version"], root, env)["stdout"]
            if not result["rustc_version"].startswith("rustc 1.96.0 ") or not result[
                "cargo_version"
            ].startswith("cargo 1.96.0 "):
                raise ValueError("Probe requires actual Rust/Cargo 1.96.0 executables")
            assert {name: sha(root / name) for name in INPUTS} == original
            result["build"] = run(
                [
                    str(paths["cargo"]),
                    "build",
                    "--offline",
                    "--locked",
                    "--bin",
                    "peeky-pagination",
                ],
                root,
                env,
                timeout=60,
            )
            binary = root / "target/debug/peeky-pagination"
            if platform.system() == "Darwin":
                bundles = list((root / "target/debug/deps").glob("peeky_pagination-*.dSYM"))
                if len(bundles) != 1:
                    raise ValueError("Expected exactly one Cargo application dSYM")
                binary = bundles[0].with_suffix("")
                symbols = bundles[0] / "Contents/Resources/DWARF" / binary.name
            else:
                symbols = binary
            assert {name: sha(root / name) for name in INPUTS} == original
            line_dump = _metadata(
                str(paths["dwarfdump"]), ["--debug-line", str(symbols)], root, env
            )
            result["line_metadata"] = {
                "bytes": len(line_dump.encode()),
                "sha256": hashlib.sha256(line_dump.encode()).hexdigest(),
                "versions": sorted(set(re.findall(r"version: (\d+)", line_dump))),
                "declared_source_present": 'name: "src/main.rs"' in line_dump
                or 'name: "main.rs"' in line_dump,
            }
            assert result["line_metadata"]["versions"] == ["4"]
            assert result["line_metadata"]["declared_source_present"]
            protected = [root / name for name in INPUTS] + [binary]
            protected += [p for p in root.rglob("*.dSYM/**/*") if p.is_file()]
            before = {str(p.relative_to(root)): sha(p) for p in protected}
            result["artifact_hashes"] = before
            result["module_identity"] = (
                run([str(paths["dwarfdump"]), "--uuid", str(binary)], root, env)["stdout"]
                if platform.system() == "Darwin"
                else elf_build_id(binary)
            )
            result["baseline"] = run([str(binary)], root, env)["stdout"]
            source = root / "src/main.rs"
            line = next(
                i
                for i, text in enumerate(source.read_text().splitlines(), 1)
                if "// OBSERVE" in text
            )
            dap = DAP((str(paths["adapter"]),), env=env)
            try:
                result["capabilities"] = dap.request(
                    "initialize",
                    {
                        "adapterID": "lldb",
                        "pathFormat": "path",
                        "linesStartAt1": True,
                        "columnsStartAt1": True,
                        "sourceInitFile": False,
                        "$__lldb_sourceInitFile": False,
                    },
                )
                launch = dap.send(
                    "launch",
                    {
                        "program": str(binary),
                        "cwd": str(root),
                        "disableASLR": False,
                        "shellExpandArguments": False,
                        "console": "internalConsole",
                        "enableAutoVariableSummaries": False,
                        "initCommands": ["settings set target.load-script-from-symbol-file false"],
                    },
                )
                dap.wait_event({"initialized"}, failed_request=launch)
                result["breakpoints"] = dap.request(
                    "setBreakpoints",
                    {"source": {"path": str(source)}, "breakpoints": [{"line": line}]},
                )
                dap.request("configurationDone")
                dap.response(launch)
                stop = dap.wait_event({"stopped", "terminated"})
                assert stop["event"] == "stopped", stop["event"]
                tid = stop["body"]["threadId"]
                frame = dap.request("stackTrace", {"threadId": tid, "levels": 10})["stackFrames"][0]
                result["frame"] = frame
                assert (
                    Path(frame["source"]["path"]).resolve() == source and frame["line"] == line
                ), frame
                result["modules"] = dap.request("modules", {"startModule": 0, "moduleCount": 32})
                identity = result["module_identity"]
                if platform.system() == "Darwin":
                    identity = (
                        re.search(r"UUID: ([A-Fa-f0-9-]+)", identity)
                        .group(1)
                        .replace("-", "")
                        .upper()
                    )
                NativeSession.validate_debugger(
                    SimpleNamespace(binary=binary, symbols=symbols, module_identity=identity),
                    dap,
                    frame,
                )
                result["module_verified"] = True
                scopes = dap.request("scopes", {"frameId": frame["id"]})["scopes"]
                local = next(
                    s
                    for s in scopes
                    if s.get("presentationHint") == "locals" or s["name"] == "Locals"
                )
                values = dap.request(
                    "variables", {"variablesReference": local["variablesReference"]}
                )["variables"]
                result["locals"] = values
                variables = {v["name"]: v for v in values}
                assert (
                    variables["start"]["type"] in ("i32", "int")
                    and variables["start"]["value"] == "3"
                ), variables["start"]
                nodes = [0]

                def children(variable, depth=0):
                    ref = variable.get("variablesReference", 0)
                    if not ref or depth >= 4:
                        return []
                    values = dap.request("variables", {"variablesReference": ref})["variables"]
                    nodes[0] += len(values)
                    if len(values) > 64 or nodes[0] > 256:
                        raise ValueError("Raw Rust inspection budget exceeded")
                    return [{**v, "children": children(v, depth + 1)} for v in values]

                result["raw_children"] = {
                    name: children(variables[name]) for name in ("request", "mode", "some", "none")
                }
                raw = result["raw_children"]
                assert {v["name"]: v["value"] for v in raw["request"]} == {
                    "page": "1",
                    "page_size": "3",
                    "total": "8",
                }
                assert variables["mode"]["value"] == "First", variables["mode"]

                def flattened(values):
                    for value in values:
                        yield value
                        yield from flattened(value["children"])

                some_fields = list(flattened(raw["some"]))
                none_fields = list(flattened(raw["none"]))
                assert any(v["name"] == "$discr$" and v["value"] == "1" for v in some_fields), (
                    some_fields
                )
                assert any(v["name"] == "__0" and v["value"] == "3" for v in some_fields), (
                    some_fields
                )
                assert any(v["name"] == "$discr$" and v["value"] == "0" for v in none_fields), (
                    none_fields
                )
                result["raw_shape_qualified"] = True
                result["inactive_variant_warning"] = (
                    "Raw Option exposes inactive variant storage; only selected discriminant/payload is meaningful. None payload is not interpreted."
                )
                scopes = dap.request("scopes", {"frameId": frame["id"]})["scopes"]
                local = next(
                    s
                    for s in scopes
                    if s.get("presentationHint") == "locals" or s["name"] == "Locals"
                )
                current = dap.request(
                    "variables", {"variablesReference": local["variablesReference"]}
                )["variables"]
                scalar = next(v for v in current if v["name"] == "start")
                assert scalar["type"] in ("i32", "int") and scalar["value"] == "3"
                result["mutation"] = dap.request(
                    "setVariable",
                    {
                        "variablesReference": local["variablesReference"],
                        "name": "start",
                        "value": "0",
                    },
                )
                assert result["mutation"].get("value", result["mutation"].get("result")) == "0"
                dap.request("continue", {"threadId": tid})
                dap.wait_event({"terminated"}, after=stop["cursor"])
                result["output"] = dap.snapshot_output()[0]
                assert "PAGE:[40, 50, 60]" in result["baseline"]
                assert "PAGE:[10, 20, 30]" in result["output"]
                assert {str(p.relative_to(root)): sha(p) for p in protected} == before
                assert {name: sha(ROOT / "examples/rust" / name) for name in INPUTS} == original
                assert all(
                    sha(paths[name]) == identity["sha256"]
                    for name, identity in result["tools"].items()
                )
                result["unchanged"] = True
            finally:
                result["event_summary"] = [
                    {"event": event.get("event"), "cursor": event.get("cursor")}
                    for event in list(dap.events)[-100:]
                ]
                result["last_output"] = dap.snapshot_output()[0][-16000:]
                pids = [
                    e["body"]["systemProcessId"]
                    for e in dap.events
                    if e.get("event") == "process" and e.get("body", {}).get("systemProcessId")
                ]
                dap.close()
                for pid in pids:
                    deadline = time.monotonic() + 3
                    while True:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Rust probe debuggee survived cleanup")
                        time.sleep(0.02)
                result["debuggee_pids_reaped"] = pids
                result["adapter_exit_code"] = dap.process.returncode
            result["passed"] = True
        result["temporary_build_removed"] = not root.exists()
    except Exception as error:
        result["error"] = repr(error)
        raise
    finally:
        result["seconds"] = time.monotonic() - started
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cargo", "rustc", "adapter", "dwarfdump", "linker"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--dsymutil")
    parser.add_argument("--sysroot")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    probe(args)


if __name__ == "__main__":
    main()
