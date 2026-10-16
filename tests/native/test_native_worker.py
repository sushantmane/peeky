"""Real LLDB C worker conformance; optional locally, required in the native CI lane."""

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest

from peeky.client import Client
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def native_config():
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    return {
        "sources": ("branch_bug.c",),
        "compiler": os.environ.get("PEEKY_NATIVE_CC", "clang"),
        "adapter": os.environ["PEEKY_NATIVE_DAP"],
        "dwarfdump": os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        "dsymutil": os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        "sysroot": os.environ.get("PEEKY_NATIVE_SYSROOT"),
    }


@pytest.fixture
def native_worker(tmp_path, native_config, monkeypatch):
    from peeky.native import NativeSession, NativeTarget

    validate_debugger = NativeSession.validate_debugger

    def diagnose_module(self, dap, frame):
        try:
            return validate_debugger(self, dap, frame)
        except RuntimeError:
            diagnostic = {
                "expected_identity": self.module_identity,
                "expected_binary": str(self.binary),
                "expected_symbols": str(self.symbols),
                "frame": frame,
            }
            try:
                diagnostic["actual_modules"] = dap.response(
                    dap.send("modules", {"startModule": 0, "moduleCount": 16}), timeout=2
                )
            except Exception as error:
                diagnostic["modules_error"] = str(error)
            print(
                "NATIVE MODULE DIAGNOSTIC: " + json.dumps(diagnostic, default=str)[:16000],
                flush=True,
            )
            raise

    monkeypatch.setattr(NativeSession, "validate_debugger", diagnose_module)

    source = tmp_path / "branch_bug.c"
    shutil.copyfile(ROOT / "examples/c/branch_bug.c", source)
    lines = source.read_text().splitlines()
    first = next(i for i, line in enumerate(lines, 1) if "/* FIRST */" in line)
    second = next(i for i, line in enumerate(lines, 1) if "/* SECOND */" in line)
    servers = []

    def start(**overrides):
        target = NativeTarget(**{**native_config, **overrides})
        server = make_server(tmp_path, "native-test-secret-token-123", 0, adapter=target)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        client = Client(f"http://127.0.0.1:{server.server_port}", "native-test-secret-token-123")
        return client, server.worker

    yield start, source, first, second
    for server, thread in servers:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def wait(client, session, state):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.call("wait", **session, timeout=1)
        if result["state"] == state:
            return result
        assert result["state"] == "running", result
    pytest.fail(f"native target did not reach {state}")


def observed(snapshot):
    return {v["name"]: v for s in snapshot["scopes"] for v in s["variables"]}


def launch(client, first, second=None):
    points = [{"line": first}]
    if second:
        points.append({"line": second})
    try:
        started = client.call("launch", program="branch_bug.c", breakpoints=points)
    except HTTPError as error:
        pytest.fail(f"native launch failed: {error.read().decode()}")
    session = {"session_id": started["session_id"]}
    stop = wait(client, session, "stopped")
    context = dict(session, stop_id=stop["stop_id"])
    try:
        snapshot = client.call("inspect", **context)
    except HTTPError as error:
        pytest.fail(
            f"native inspect failed ({error.code}): {error.read(16000).decode(errors='replace')}"
        )
    assert snapshot["frames"][0]["line"] == first
    events = client.call("report", **session)["breakpoint_events"]
    verified = any(p.get("verified") and p.get("line") == first for p in started["breakpoints"])
    verified |= any(
        e["breakpoint"].get("verified") and e["breakpoint"].get("line") == first for e in events
    )
    assert verified
    return started, session, context, snapshot


def fail_http(code, client, operation, **args):
    with pytest.raises(HTTPError) as caught:
        client.call(operation, **args)
    assert caught.value.code == code
    return json.load(caught.value)["error"]["message"]


def gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"native target {pid} survived close")


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_c_http_scalar_intervention_and_stale_stop(native_worker, monkeypatch):
    monkeypatch.setenv("DEBUG_TOKEN", "never-pass-to-native-target")
    start, source, first, second = native_worker
    client, worker = start()
    initial_hash = hash_file(source)
    started, session, old, snapshot = launch(client, first, second)
    assert started["artifact_manifest_id"]
    current = observed(snapshot)
    assert current["value"]["value"] == "0"
    assert current["pointer"]["type"] == "int *"
    assert current["pointer"]["variablesReference"] > 0
    assert current["items"]["type"] == "int[3]"
    assert current["items"]["variablesReference"] > 0
    assert current["token_present"]["value"] == "0"
    assert all(s["name"].lower() != "registers" for s in snapshot["scopes"])
    pid = int(current["pid"]["value"])
    binary = worker.runtime.binary
    binary_hash = hash_file(binary)
    ref = next(
        s["reference"]
        for s in snapshot["scopes"]
        if any(v["name"] == "value" for v in s["variables"])
    )
    old_ref = ref
    snapshot = client.call("inspect", **old)
    ref = next(
        s["reference"]
        for s in snapshot["scopes"]
        if any(v["name"] == "value" for v in s["variables"])
    )
    assert ref != old_ref
    fail_http(400, client, "set_variable", **old, reference=old_ref, name="value", value="99")
    fail_http(400, client, "set_variable", **old, reference=ref, name="pointer", value="0")
    change = client.call("set_variable", **old, reference=ref, name="value", value="5000")
    assert change["before"] == "0" and change["after"] == "5000"
    assert hash_file(binary) == binary_hash and hash_file(source) == initial_hash
    client.call("continue", **old)
    stop = wait(client, session, "stopped")
    assert stop["stop_id"] != old["stop_id"]
    assert "Stale stop" in fail_http(
        400, client, "set_variable", **old, reference=ref, name="value", value="99"
    )
    context = dict(session, stop_id=stop["stop_id"])
    fail_http(400, client, "set_variable", **context, reference=ref, name="value", value="99")
    assert observed(client.call("inspect", **context))["value"]["value"] == "5000"
    client.call("continue", **context)
    wait(client, session, "terminated")
    assert (
        "RESULT=success value=5000 array=4 pointer=5000"
        in client.call("report", **session)["output"]
    )
    client.call("close", **session)
    gone(pid)
    assert hash_file(source) == initial_hash


@pytest.mark.parametrize("tamper", ["source", "copied-source", "binary", "symbols"])
def test_native_integrity_drift_invalidates_before_mutation(native_worker, tamper):
    start, source, first, _ = native_worker
    client, worker = start()
    _, session, context, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    if tamper == "source":
        path = source
    elif tamper == "copied-source":
        path = runtime.prepared.workspace / "branch_bug.c"
    elif tamper == "symbols":
        path = runtime.symbols
    else:
        path = runtime.binary
    changed_bytes = path.read_bytes() + b"changed"
    replacement = path.with_name(path.name + ".replacement")
    replacement.write_bytes(changed_bytes)
    replacement.chmod(path.stat().st_mode)
    # Linux prohibits writing a running executable; replacement exercises drift
    # of the named artifact while the old inode may remain loaded.
    replacement.replace(path)
    assert path.read_bytes() == changed_bytes
    ref = snapshot["scopes"][0]["reference"]
    fail_http(500, client, "set_variable", **context, reference=ref, name="value", value="5000")
    assert worker.session is None
    assert not runtime.prepared.workspace.exists()
    gone(pid)
    fail_http(400, client, "report", **session)


def test_native_stopped_close_reaps_target(native_worker):
    start, _, first, _ = native_worker
    client, worker = start()
    _, session, _, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    client.call("close", **session)
    gone(pid)
    assert not runtime.prepared.workspace.exists()


def test_native_missing_symbols_fails_launch(native_worker, native_config, tmp_path):
    start, _, first, _ = native_worker
    compiler = shutil.which(native_config["compiler"])
    assert compiler
    wrapper = tmp_path / "compiler-without-symbols"
    wrapper.write_text(
        f"#!{sys.executable}\nimport os, sys\nos.execv({compiler!r}, [{compiler!r}, *[arg for arg in sys.argv[1:] if not arg.startswith('-g')]])\n"
    )
    wrapper.chmod(0o755)
    client, worker = start(compiler=str(wrapper))
    with pytest.raises(HTTPError) as caught:
        client.call("launch", program="branch_bug.c", breakpoints=[{"line": first}])
    message = json.load(caught.value)["error"]["message"]
    assert any(
        word in message.lower()
        for word in ("debug", "symbol", "dwarf", "line table", "line information")
    ), message
    assert worker.session is None and worker.runtime is None


def test_native_skips_hostile_home_init_file(native_worker, native_config, tmp_path, monkeypatch):
    from peeky.dap import DAP

    home = tmp_path / "hostile-home"
    home.mkdir()
    marker = tmp_path / "lldbinit-executed"
    (home / ".lldbinit").write_text(f"script open({str(marker)!r}, 'w').write('loaded')\n")
    monkeypatch.setenv("HOME", str(home))
    # Positive control: verify this LLDB really consults our fixture HOME.
    control = DAP(command=(native_config["adapter"],), env=dict(os.environ))
    try:
        control.request(
            "initialize",
            {
                "adapterID": "lldb",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                "sourceInitFile": True,
                "$__lldb_sourceInitFile": True,
            },
        )
        assert marker.read_text() == "loaded"
    finally:
        control.close()
    marker.unlink()
    start, _, first, _ = native_worker
    client, _ = start()
    _, session, _, _ = launch(client, first)
    assert not marker.exists()
    client.call("close", **session)


def test_native_adapter_death_reaps_stopped_target(native_worker):
    start, _, first, _ = native_worker
    client, worker = start()
    _, session, context, snapshot = launch(client, first)
    pid = int(observed(snapshot)["pid"]["value"])
    runtime = worker.runtime
    adapter = worker.dap.process
    adapter.kill()
    adapter.wait(timeout=3)
    fail_http(500, client, "inspect", **context)
    assert worker.session is None
    assert not runtime.prepared.workspace.exists()
    gone(pid)
    fail_http(400, client, "report", **session)


def test_native_arguments_are_literal_and_keep_empty_values(native_worker):
    start, _, first, _ = native_worker
    client, _ = start(arguments=("two words", "", "*", "$HOME", "it's"))
    _, session, context, _ = launch(client, first)
    client.call("continue", **context)
    wait(client, session, "terminated")
    output = client.call("report", **session)["output"]
    for expected in ("ARG:9:two words", "ARG:0:", "ARG:1:*", "ARG:5:$HOME", "ARG:4:it's"):
        assert expected in output
    client.call("close", **session)


CRASH = """#include <stdio.h>

int main(void) {
    int *pointer = NULL;
    int value = 7;
    printf("before %d\\n", value); /* BREAK */
    fflush(stdout);
    *pointer = value;
    return 0;
}
"""


def test_pause_guard_resumes_breakpoints_but_never_crash_stops(native_worker, tmp_path):
    start, _, _, _ = native_worker
    (tmp_path / "crash.c").write_text(CRASH)
    line = next(i for i, text in enumerate(CRASH.splitlines(), 1) if "BREAK" in text)
    client, worker = start(sources=("crash.c",))
    started = client.call("launch", program="crash.c", breakpoints=[{"line": line}], max_pause=0.5)
    session = {"session_id": started["session_id"]}
    first = wait(client, session, "stopped")
    assert first["reason"] == "breakpoint" and "resume_in_s" in first
    deadline = time.monotonic() + 15
    while not worker.auto_resumed and time.monotonic() < deadline:
        time.sleep(0.1)
        worker.guard_pause()
    crash = wait(client, session, "stopped")
    assert crash["reason"] != "breakpoint" and "resume_in_s" not in crash
    assert crash["last_auto_resumed"]["reason"] == "breakpoint"
    for _ in range(15):
        time.sleep(0.1)
        worker.guard_pause()
    assert worker.state == "stopped" and worker.stop_id == crash["stop_id"]
    assert len(worker.auto_resumed) == 1
    snapshot = client.call("inspect", **session, stop_id=crash["stop_id"])
    assert snapshot["frames"][0]["line"] == line + 2


def test_caller_frames_outside_the_program_are_request_errors(native_worker):
    start, _, first, _ = native_worker
    client, _ = start()
    _, session, context, snapshot = launch(client, first)
    frames = client.call("inspect", **context)["frames"]
    outside = [
        frame
        for frame in frames[1:]
        if not str(frame.get("source", {}).get("path", "")).endswith("/branch_bug.c")
    ]
    if not outside:
        pytest.skip("the debugger listed no frame outside the program")
    with pytest.raises(HTTPError) as caught:
        client.call("inspect", **context, frame_id=outside[-1]["id"])
    error = json.load(caught.value)["error"]
    assert caught.value.code == 400 and error["session_closed"] is False
    assert "not in the prepared program's sources" in error["message"]
    assert client.call("inspect", **context)["frames"][0]["line"] == first
    client.call("close", **session)


def test_cpp_programs_link_the_cpp_standard_library(tmp_path, native_config, capsys):
    from peeky.cli import main

    source = tmp_path / "words.cpp"
    source.write_text(
        "#include <iostream>\n#include <string>\n#include <vector>\n"
        "int main() {\n"
        '  std::vector<std::string> words{"a", "bb"};\n'
        "  int n = static_cast<int>(words.size());\n"
        "  std::cout << n << words[1] << std::endl;\n"
        "  return 0;\n"
        "}\n"
    )
    code = main(["probe", str(source), "--at", "7", "--capture", "n", "--json", "--no-save"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "2" and "2bb" in result["output"]


@pytest.mark.skipif(sys.platform != "darwin", reason="xcrun stubs are macOS-only")
def test_macos_builds_without_a_sysroot_use_the_default_sdk(tmp_path, native_config):
    from peeky.native import NativeTarget

    shutil.copyfile(ROOT / "examples/c/branch_bug.c", tmp_path / "branch_bug.c")
    stub = {"compiler": "/usr/bin/clang", "sysroot": None}
    runtime = NativeTarget(**{**native_config, **stub}).prepare(tmp_path / "branch_bug.c", tmp_path)
    runtime.close()


def test_mains_that_are_not_compiled_do_not_split_a_program(tmp_path, native_config, capsys):
    from peeky.cli import main

    (tmp_path / "main.c").write_text(
        "#include <stdio.h>\nint add(int a, int b);\n"
        'int main(void) {\n  int total = add(2, 3);\n  printf("%d\\n", total);\n'
        "  return 0;\n}\n"
    )
    (tmp_path / "util.c").write_text(
        "int add(int a, int b) { return a + b; }\n"
        "#ifdef UTIL_SELFTEST\nint main(void) { return add(1, 1) != 2; }\n#endif\n"
        "/*\nint main(void) { return 0; }\n*/\n"
    )
    code = main(["probe", str(tmp_path / "main.c"), "--at", "5", "--capture", "total", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert result["rows"][0]["value"] == "5"


def test_threads_stopping_together_are_one_stop_not_stale_new_ones(tmp_path, native_config):
    from peeky.native import NativeTarget
    from peeky.probe import Probe, run_probe

    source = tmp_path / "threads.c"
    source.write_text(
        "#include <pthread.h>\n#include <stdatomic.h>\n#include <stdio.h>\n"
        "static atomic_int ready;\n"
        "static void *work(void *arg) {\n"
        "  int id = (int)(long)arg;\n"
        "  atomic_fetch_add(&ready, 1);\n"
        "  while (atomic_load(&ready) < 4) {}\n"
        "  int seen = id; /* HIT */\n"
        "  return (void *)(long)seen;\n"
        "}\n"
        "int main(void) {\n"
        "  pthread_t t[4];\n"
        "  for (long i = 0; i < 4; i++) pthread_create(&t[i], 0, work, (void *)i);\n"
        "  for (int i = 0; i < 4; i++) pthread_join(t[i], 0);\n"
        '  puts("done");\n'
        "  return 0;\n"
        "}\n"
    )
    target = NativeTarget(**{**native_config, "sources": ("threads.c",)})
    for _ in range(3):
        result = run_probe(tmp_path, Probe("threads.c", 9, ("id",), hits=4), adapter=target)
        # lldb reports one `stopped` per thread at the breakpoint: each is a real hit,
        # served in turn before the program resumes, never dropped or replayed later.
        assert result["status"] == "completed", result.get("error")
        assert result["hits"] == 4 and not result["warnings"], result["warnings"]
        assert sorted(row.get("value") for row in result["rows"]) == ["0", "1", "2", "3"]
        assert "done" in result["output"]


def test_headers_read_from_outside_the_workspace_are_provenance(tmp_path, native_config):
    from peeky.errors import classify
    from peeky.native import NativeTarget

    outside = tmp_path / "outside" / "limit.h"
    outside.parent.mkdir()
    outside.write_text("#define LIMIT 3\n")
    root = tmp_path / "program"
    root.mkdir()
    (root / "main.c").write_text(
        f'#include <stdio.h>\n#include "{outside}"\n'
        'int main(void) {\n  int limit = LIMIT;\n  printf("%d\\n", limit);\n  return 0;\n}\n'
    )
    target = NativeTarget(**{**native_config, "sources": ("main.c",)})
    session = target.prepare(root / "main.c", root)
    try:
        assert str(outside.resolve()) in session.external_inputs
        assert any(path.endswith("/stdio.h") for path in session.external_inputs)
        session.assert_current()
        outside.write_text("#define LIMIT 4\n")
        with pytest.raises(RuntimeError, match="outside the workspace") as caught:
            session.assert_current()
        assert classify(caught.value)["code"] == "PROVENANCE_CHANGED"
    finally:
        session.close()


def test_compiled_programs_take_per_run_arguments(tmp_path, native_config, capsys):
    from peeky.cli import main

    shutil.copyfile(ROOT / "examples/c/pagination_bug.c", tmp_path / "pagination_bug.c")
    shutil.copyfile(ROOT / "examples/c/pagination.h", tmp_path / "pagination.h")
    probe = ["probe", str(tmp_path / "pagination_bug.c"), "--at", "25", "--json", "--no-save"]
    code = main([*probe, "--capture", "start,page", "--arg", "2"])
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["6", "2"]
    assert "PAGE:[70, 80]" in result["output"] and result["probe"]["args"] == ["2"]
    assert result["program_args"] == result["target_identity"]["arguments"] == ["2"]
    code = main([*probe, "--capture", "page", "--arg", "bad\x00arg"])
    assert code == 2 or json.loads(capsys.readouterr().out)["status"] == "failed"


def test_describe_lists_each_functions_span_and_locals(tmp_path, native_config, capsys):
    from peeky.cli import main

    for language in ("c", "cpp"):
        shutil.copytree(ROOT / "examples" / language, tmp_path / language)
    for path, function, local in (
        ("c/pagination_bug.c", "main", "start"),
        ("cpp/pagination_bug.cpp", "main", "request"),
    ):
        assert main(["describe", str(tmp_path / path), "--json"]) == 0
        program = Path(path).name
        manifest = json.loads(capsys.readouterr().out)
        scope = next(s for s in manifest["scopes"] if s["scope"] == function)
        assert scope["source"] == program and local in scope["names"]
        first, last = scope["lines"]
        lines = manifest["breakpoint_lines"][program]
        assert first in lines and last in lines and first < last
        # Only the program's own functions: nothing from the SDK or the C++ library.
        assert {s["source"] for s in manifest["scopes"]} <= set(manifest["breakpoint_lines"])
        assert main(["describe", str(tmp_path / path)]) == 0
        assert f"names in {function} ({program}:{first}-{last}):" in capsys.readouterr().out
