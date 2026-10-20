"""Real LLDB C worker conformance; optional locally, required in the native CI lane."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
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
        # #93 (eval 4, C): each local's type, as the debug information names it.
        if program.endswith(".c"):
            assert scope["types"]["start"] == "int"
        first, last = scope["lines"]
        lines = manifest["breakpoint_lines"][program]
        assert first in lines and last in lines and first < last
        # Only the program's own functions: nothing from the SDK or the C++ library.
        assert {s["source"] for s in manifest["scopes"]} <= set(manifest["breakpoint_lines"])
        assert main(["describe", str(tmp_path / path)]) == 0
        text = capsys.readouterr().out
        assert f"names in {function} ({program}:{first}-{last}):" in text
        assert program != "pagination_bug.c" or "start (int)" in text


def test_native_sessions_step_and_report_leaving_the_program(
    tmp_path, native_config, capsys, monkeypatch
):
    from peeky.cli import main

    shutil.copytree(ROOT / "examples" / "c", tmp_path / "c")
    program = str(tmp_path / "c" / "pagination_bug.c")
    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    lines = (tmp_path / "c" / "pagination_bug.c").read_text().splitlines()
    last = max(i for i, text in enumerate(lines, 1) if "return 0" in text)
    assert main(["session", "start", program, "--at", str(last)]) == 0
    capsys.readouterr()
    # Stepping past main's end leaves the program's sources: said so, nothing read,
    # and the session stays usable.
    state = None
    for _ in range(4):
        assert main(["session", "step", "--json"]) == 0
        state = json.loads(capsys.readouterr().out)
        if state.get("outside_program") or state["state"] != "stopped":
            break
    assert state["state"] == "stopped" and state["outside_program"] is True
    assert state["returns_to_program"] is False
    assert main(["session", "inspect"]) == 2
    assert "outside the program's sources" in capsys.readouterr().err
    code = main(["session", "step", "--out", "--json"])
    reply = json.loads(capsys.readouterr().out)
    if code == 2:
        # macOS: dyld's start is the outermost frame; the debugger refuses, and the
        # session stays.
        assert "refused to step out" in reply["error"]["message"]
    else:
        # Linux: libc's callers remain, so the step runs (still outside, or to the end).
        assert reply["state"] == "terminated" or reply.get("outside_program") is True
    if reply.get("state") != "terminated":
        assert main(["session", "continue"]) == 0
        assert "terminated" in capsys.readouterr().out
    assert main(["session", "close"]) == 0


THREADS = (
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


def test_steps_wait_until_threads_stopped_together_have_resumed(
    tmp_path, native_config, capsys, monkeypatch
):
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "threads.c").write_text(THREADS)
    assert main(["session", "start", str(tmp_path / "threads.c"), "--at", "9"]) == 0
    capsys.readouterr()
    # Unserved co-stops, and served ones whose threads the debugger still holds at their
    # old stops (it would re-report them when a step ends): refused, session kept.
    for _ in range(2):
        assert main(["session", "step", "--json"]) == 2
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "INVALID_REQUEST" and "Other threads" in error["message"]
        assert main(["session", "continue"]) == 0
        assert "stopped" in capsys.readouterr().out
    assert main(["session", "continue", "--to-exit"]) == 0
    assert main(["session", "report"]) == 0 and "done" in capsys.readouterr().out
    assert main(["session", "close"]) == 0


CALLS = """#include <stdio.h>

int twice(int x) {
    int r = x * 2;
    return r;
}

int main(void) {
    int a = twice(1);
    int b = twice(2);
    printf("SUM:%d\\n", a + b);
    return 0;
}
"""


def test_runs_to_lines_and_exit_survive_bad_lines_and_interrupted_steps(
    tmp_path, native_config, capsys, monkeypatch
):
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "calls.c").write_text(CALLS)
    assert main(["session", "start", str(tmp_path / "calls.c"), "--at", "4"]) == 0
    capsys.readouterr()
    # A line without code is refused before anything runs; the stop is kept.
    assert main(["session", "continue", "--to", "7", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_BREAKPOINT"
    assert main(["session", "get", "x"]) == 0 and "x: int = 1" in capsys.readouterr().out
    # Stepping over twice(2) is interrupted by the breakpoint inside it; --to-exit then
    # still runs to the end instead of stopping where LLDB finishes that step.
    for kind in (["--out"], [], []):
        assert main(["session", "step", *kind]) == 0
    assert "calls.c:4" in capsys.readouterr().out
    assert main(["session", "continue", "--to-exit"]) == 0
    assert "terminated" in capsys.readouterr().out
    assert main(["session", "report"]) == 0 and "SUM:6" in capsys.readouterr().out
    assert main(["session", "close"]) == 0


RACE = """#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
static atomic_int go;
static int mark(int id) {
  int seen = id * 10; /* MARK */
  return seen;
}
static void release(void) { atomic_store(&go, 1); }
static void *b_main(void *arg) {
  while (!atomic_load(&go)) {}
  printf("B%d\\n", mark(2));
  return NULL;
}
static void *a_main(void *arg) {
  int a = mark(1);
  release(); /* RELEASE */
  printf("A%d\\n", a);
  return NULL;
}
int main(void) {
  pthread_t b, a;
  pthread_create(&b, 0, b_main, 0);
  pthread_create(&a, 0, a_main, 0);
  pthread_join(a, 0);
  pthread_join(b, 0);
  return 0;
}
"""


def test_a_step_interrupted_by_another_threads_hit_can_still_step(
    tmp_path, native_config, capsys, monkeypatch
):
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "race.c").write_text(RACE)
    lines = RACE.splitlines()
    mark = next(i for i, text in enumerate(lines, 1) if "MARK" in text)
    release = next(i for i, text in enumerate(lines, 1) if "RELEASE" in text)
    for _ in range(3):
        assert main(["session", "start", str(tmp_path / "race.c"), "--at", str(mark)]) == 0
        assert main(["session", "continue", "--to", str(release)]) == 0
        # Stepping over release() lets thread B hit the breakpoint while A steps: LLDB
        # reports both in one all-threads stop. Whichever is served, stepping works.
        assert main(["session", "step"]) == 0
        capsys.readouterr()
        assert main(["session", "step", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] in ("stopped", "terminated")
        assert main(["session", "continue", "--to-exit"]) == 0
        assert main(["session", "report"]) == 0
        output = capsys.readouterr().out
        assert "A10" in output and "B20" in output
        assert main(["session", "close"]) == 0


def test_describe_leaves_out_types_a_program_cannot_write(tmp_path, native_config, capsys):
    # Review of #97: dwarfdump names unnamed types by their tag (`structure `).
    from peeky.cli import main

    (tmp_path / "anon.c").write_text(
        "#include <stdio.h>\n"
        "struct structure { int s; };\n"
        "enum enumeration { ONE };\n"
        "typedef struct { int c; } class_t;\n"
        "typedef int *ip;\n"
        "static float dot(const float *restrict a, int *restrict b, int n, ip restrict t,\n"
        "                 int (*restrict pa)[3], int (**restrict fns)(int)) {\n"
        "    int (*fs[1])(int) = {0};\n"
        "    float s = (float)(*t + (*pa)[0] + (fns != 0) + (fs[0] != 0));\n"
        "    for (int i = 0; i < n; i++) s += a[i] * b[i];\n"
        "    return s;\n"
        "}\n"
        "int main(void) {\n"
        "    struct { int x; } inl = {1};\n"
        "    union { int a; float b; } un = {2};\n"
        "    struct { int y; } *ptr = 0;\n"
        "    struct { int z; } arr[3] = {{0}};\n"
        "    struct { int w; } *const cptr = 0;\n"
        "    struct { int v; } *volatile vptr = 0;\n"
        "    const struct { int u; } cst = {3};\n"
        "    struct structure named = {4}, *named_ptr = &named;\n"
        "    enum enumeration en = ONE;\n"
        "    class_t ct = {5};\n"
        "    float xs[2] = {1, 2};\n"
        "    int ys[2] = {3, 4};\n"
        "    int three[3] = {0};\n"
        "    int plain = inl.x + un.a + (ptr != 0) + arr[0].z + (cptr != 0) + (vptr != 0) + cst.u\n"
        "        + named_ptr->s + en + ct.c + (int)dot(xs, ys, 2, ys, &three, 0);\n"
        '    printf("%d\\n", plain);\n'
        "    return 0;\n"
        "}\n"
    )
    (tmp_path / "lam.cpp").write_text(
        "#include <cstdio>\n"
        "struct { struct In { int z; } in; int q; } wrap;\n"
        "int main() {\n"
        "    decltype(wrap)::In *inner = &wrap.in;\n"
        "    int base = 3 + inner->z;\n"
        "    auto add = [base](int v) { return v + base; };\n"
        '    std::printf("%d\\n", add(4));\n'
        "    return 0;\n"
        "}\n"
    )
    named = {
        "plain": "int",
        "named": "structure",
        "named_ptr": "structure *",
        "en": "enumeration",
        "ct": "class_t",
        "xs": "float[2]",
        "ys": "int[2]",
    }
    unnamed = ("inl", "un", "ptr", "arr", "cptr", "vptr", "cst")
    for name, function, typed, untyped in (
        ("anon.c", "main", named, unnamed),
        # dwarfdump prints a restrict pointer as `restrict ` alone.
        (
            "anon.c",
            "dot",
            {"a": "const float *restrict", "b": "int *restrict", "n": "int", "t": "ip restrict"},
            # restrict inside parentheses, and dwarfdump's cut-short `int (*[1]`.
            ("pa", "fns", "fs"),
        ),
        # A lambda, and a type declared inside an unnamed struct (`structure ::In *`).
        ("lam.cpp", "main", {"base": "int"}, ("add", "inner")),
    ):
        assert main(["describe", str(tmp_path / name), "--json"]) == 0
        scopes = json.loads(capsys.readouterr().out)["scopes"]
        scope = next(s for s in scopes if s["scope"] == function)
        assert set(untyped) <= set(scope["names"])
        assert {k: v for k, v in scope.get("types", {}).items() if k in typed or k in untyped} == (
            typed
        ), scope
        # dwarfdump's forms for unnamed types, in any scope (the lambda's own `this`).
        unwritable = re.compile(
            r"  |restrict $|[)\]] restrict|(?:structure|union|class|enumeration) (?:$|\[|::)"
        )
        assert not any(
            unwritable.search(kind) for s in scopes for kind in s.get("types", {}).values()
        ), scopes


def test_describe_gives_the_lines_of_locals_declared_in_nested_blocks(
    tmp_path, native_config, capsys
):
    # #107: `describe` listed a `for` loop's `i` among the function's names, though it
    # is not visible after the loop. A local declared only in nested blocks now carries
    # the lines of those blocks.
    from peeky.cli import main

    (tmp_path / "blocks.c").write_text(BLOCKS)
    assert main(["describe", str(tmp_path / "blocks.c"), "--json"]) == 0
    scopes = json.loads(capsys.readouterr().out)["scopes"]
    spans = next(s for s in scopes if s["scope"] == "average")["spans"]
    assert set(spans) == {"i", "half"}, spans
    ((first, last),) = spans["i"]
    assert first == 4 and 5 <= last <= 6, spans
    ((first, last),) = spans["half"]
    assert 7 <= first <= 8 and 9 <= last <= 10, spans
    assert main(["describe", str(tmp_path / "blocks.c")]) == 0
    text = capsys.readouterr().out
    assert "i (int, lines 4-" in text and "count (int)," in text
    # Review of #107: a block that runs an `#include`d file's code gives no span (its
    # names are visible on that file's lines too); an always-inline function's blocks
    # are read from its inlined copies.
    (tmp_path / "step.h").write_text("extra += k;\n")
    (tmp_path / "included.c").write_text(INCLUDED)
    assert main(["describe", str(tmp_path / "included.c"), "--json"]) == 0
    scopes = {s["scope"]: s for s in json.loads(capsys.readouterr().out)["scopes"]}
    assert "extra" not in scopes["main"].get("spans", {}), scopes["main"]
    assert "j" in scopes["twice"]["spans"], scopes["twice"]


def test_a_gcc_optimized_block_span_skips_rows_another_row_replaces(
    tmp_path, native_config, capsys
):
    # Review 3 of #107: GCC -O2 put a row for the loop's line 5 at the address of the
    # later block's line 15, so `tail` took the loop's lines too.
    gcc = shutil.which("gcc")
    version = subprocess.run([gcc, "--version"], capture_output=True, text=True) if gcc else None
    if version is None or "Free Software Foundation" not in version.stdout:
        pytest.skip("needs GCC")
    from peeky.cli import main

    def line_of(text, marker):
        return next(i for i, line in enumerate(text.splitlines(), 1) if marker in line)

    def described(name, text, level):
        (tmp_path / name).write_text(text)
        config = {
            "language": "c",
            "sources": [name],
            "headers": [],
            "compiler": gcc,
            "adapter": native_config["adapter"],
            "dwarfdump": native_config["dwarfdump"],
            "optimization": level,
        }
        (tmp_path / f"{level}.json").write_text(json.dumps(config))
        arguments = ["describe", str(tmp_path / name), "--config", str(tmp_path / f"{level}.json")]
        assert main([*arguments, "--json"]) == 0
        return {
            s["scope"]: s.get("spans", {}) for s in json.loads(capsys.readouterr().out)["scopes"]
        }

    spans = described("o2.c", OPTIMIZED_BLOCKS, "O2")["accumulate"]
    assert all(first >= 14 for first, _ in spans.get("tail", [])), spans
    # Review 4: a statement row a later row at its address replaces (an inlined
    # `printf`'s) runs no code either; an inlined call's line is its block's (LLDB stops
    # there, at the callee's entry, in this frame).
    words = line_of(RICH, "int words = count_words(buf);")
    spans = described("rich.c", RICH, "O2")["main"]
    assert spans["words"] and all(first >= words for first, _ in spans["words"]), spans
    neg = line_of(RICH, "return clamp(neg, -50, 0);")
    spans = described("rich.c", RICH, "Og")["classify"]
    assert any(first <= neg <= last for first, last in spans["neg"]), spans
    # Review 5: of the rows at one address, only the last runs, whatever its neighbours;
    # one of its own line stays.
    doubled = line_of(RICH, "int doubled = v * 2;")
    spans = described("rich.c", RICH, "O1")["classify"]
    assert any(first <= doubled <= last for first, last in spans["doubled"]), spans
    spans = described("calls.c", INLINED_CALLS, "O2")["work"]
    for local in ("w", "z"):
        declared = line_of(INLINED_CALLS, f"int {local} = ")
        assert spans[local] and all(first >= declared for first, _ in spans[local]), spans


OPTIMIZED_BLOCKS = """#include <stdio.h>
#include <stdlib.h>
static int accumulate(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        int r = rand() % 5;
        total += r * i;
        if (total > 50) {
            int cut = total / 2;
            printf("cut %d\\n", cut);
            total -= cut;
        }
    }
    {
        int tail = rand() & 3;
        total += tail;
    }
    return total;
}
int (*volatile fp)(int) = accumulate;
int main(int argc, char **argv) {
    int a = accumulate(argc + 4);
    int b = fp(argc + 6);
    int c = accumulate(argc + 8);
    printf("%d %d %d\\n", a, b, c);
    return 0;
}
"""

INLINED_CALLS = """#include <stdio.h>
#include <stdlib.h>

static inline int lim(int n) {
    int r = n > 5 ? 5 : n;
    return r;
}

static inline int step(int i) {
    return i + 1 + (rand() & 1);
}

static int work(int n) {
    int total = 0;
    for (int i = 0; i < lim(n); i = step(i)) {
        int sq = i * i + rand() % 3;
        printf("sq %d\\n", sq);
        total += sq;
    }
    {
        int a = lim(n + rand() % 4);
        {
            int b = a + rand() % 5;
            printf("b %d\\n", b);
            total += b;
        }
    }
    int k = 0;
    while (lim(k + n) > k) {
        int w = k * 3 + rand() % 2;
        printf("w %d\\n", w);
        total += w;
        k++;
    }
    do {
        int d = rand() % 7;
        printf("d %d\\n", d);
        total += d;
    } while (lim(total) < 3);
    if (lim(n) > 2) {
        int y = n * 2 + rand() % 3;
        printf("y %d\\n", y);
        total += y;
    }
    switch (lim(n + total)) {
    case 5: {
        int z = rand() % 9;
        printf("z %d\\n", z);
        total += z;
        break;
    }
    default:
        total += 1;
    }
    return total;
}

int main(int argc, char **argv) {
    int r = work(argc + 6);
    printf("%d\\n", r);
    return r & 1;
}
"""

RICH = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct stats { int count; long sum; int max; };

static inline int clamp(int v, int lo, int hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static void add(struct stats *s, int v) {
    s->count++;
    s->sum += v;
    if (v > s->max) {
        int old = s->max;
        s->max = v;
        if (old < 0) printf("first max %d\\n", v);
    }
}

static int count_words(const char *text) {
    int words = 0, in_word = 0;
    for (const char *p = text; *p; p++) {
        int space = (*p == ' ' || *p == '\\n');
        if (!space && !in_word) {
            words++;
        }
        in_word = !space;
    }
    return words;
}

static int classify(int v) {
    switch (v % 4) {
    case 0: {
        int doubled = v * 2;
        return clamp(doubled, 0, 100);
    }
    case 1: {
        int neg = -v;
        return clamp(neg, -50, 0);
    }
    case 2:
        return v;
    default: {
        int sq = v * v;
        for (int k = 0; k < 3; k++) {
            sq ^= rand() & k;
        }
        return sq;
    }
    }
}

static int fib(int n) {
    if (n < 2) return n;
    int a = fib(n - 1);
    int b = fib(n - 2);
    return a + b;
}

int main(int argc, char **argv) {
    struct stats s = {0, 0, -1};
    char buf[64];
    snprintf(buf, sizeof buf, "the quick %s fox", argc > 1 ? argv[1] : "brown");
    for (int i = 0; i < 6 + argc; i++) {
        int c = classify(i + rand() % 3);
        add(&s, c);
        if (c > 10) {
            int words = count_words(buf);
            printf("words %d\\n", words);
        }
    }
    int f = fib(8 + argc);
    {
        long avg = s.count ? s.sum / s.count : 0;
        printf("%d %ld %d %ld %d\\n", s.count, s.sum, s.max, avg, f);
    }
    return (int)strlen(buf) & 1;
}
"""

BLOCKS = """#include <stdio.h>
static double average(const int *values, int count) {
    int total = 0;
    for (int i = 0; i < count; i++) {
        total += values[i];
    }
    if (count > 0) {
        int half = count / 2;
        total += half * 0;
    }
    return count ? (double)total / count : 0.0;
}
int main(void) {
    int values[3] = {1, 2, 3};
    printf("%f\\n", average(values, 3));
    return 0;
}
"""

INCLUDED = """#include <stdio.h>
static inline __attribute__((always_inline)) int twice(int v) {
    int out = 0;
    for (int j = 0; j < 2; j++) {
        out += v;
    }
    return out;
}
int main(void) {
    int total = 0;
    for (int k = 0; k < 2; k++) {
        int extra = 0;
#include "step.h"
        total += extra + twice(k);
    }
    printf("%d\\n", total);
    return 0;
}
"""


def test_a_pointers_first_element_is_its_pointee(tmp_path, native_config, capsys):
    # Eval 5 dry run: `values[0]` on `const int *values` said only "children: *values".
    from peeky.cli import main

    (tmp_path / "avg.c").write_text(AVG)
    probe = ["probe", str(tmp_path / "avg.c"), "--at", "7", "--json", "--no-save", "--quiet"]
    capture = "values[0],values[1],count[0],n.count[0],n.count[1],pp[0][0],pp[0][1]"
    # Review of #100: qualified and typedef'd pointers (`int *const`, `__restrict`).
    capture += ",cp[0],rp[0],tp[0],rp[1]"
    assert main([*probe, "--capture", capture]) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    values = {name: row.get("value") for name, row in rows.items()}
    assert values["values[0]"] == "-3" and values["count[0]"] == "100"
    assert values["n.count[0]"] == values["pp[0][0]"] == values["rp[0]"] == "1"
    assert values["cp[0]"] == values["tp[0]"] == "100"
    # Review of #100: the hint names the whole path, never another variable's.
    paths = (("values[1]", "values"), ("n.count[1]", "n.count"), ("pp[0][1]", "pp[0]"))
    for name, path in (*paths, ("rp[1]", "rp")):
        assert f"; {path} is a pointer, which carries no length: {path}[0]" in rows[name]["error"]
    main(["session", "start", str(tmp_path / "avg.c"), "--at", "7"])
    capsys.readouterr()
    try:
        assert main(["session", "get", "values", "pp[0]"]) == 0
        shown = capsys.readouterr().out
        assert "values[0]: const int = -3" in shown and "pp[0][0]: int = 1" in shown
    finally:
        main(["session", "close"])
        capsys.readouterr()


def test_a_pointer_to_an_array_or_container_indexes_as_c_does(tmp_path, native_config, capsys):
    # #102: the debugger lists a pointer's pointee elements as its own children, so
    # `parr[1]` read `(*parr)[1]`, where C means the next row. Now p[0] is the pointee and
    # p[1] is refused with the selector that reads the element.
    from peeky.cli import main

    for name, text, line, capture in (
        ("rows.c", ROWS, 8, "parr[0],parr[00],parr[0][1],parr[1],parr[-1],pp[0].y,pp.y,pp[1]"),
        ("rows.cpp", VECTOR, 7, "vp[0],vp[0][1],vp[1]"),
        (
            "aliases.cpp",
            ALIASES,
            25,
            "rp[0][1],rp[1],crp[0][1],rr[1],vr[1],bigp[0][450],bigp[-1],ep[0],np[0],"
            "garbage[0],tp[1],dp[0],later[0],pc[1],vref[0],avp[0][1]",
        ),
        ("names.cpp", SAME_NAMES, 23, "r[1],r[0][1],rf[0],rf[1],v[0],v[1],h.r[0],g.v[0][1],g.v[1]"),
        ("inline.cpp", INLINE_NAMES, 20, "rf[0],rf[1],r[1],r[0][1]"),
        ("local.cpp", LOCAL_CLASS, 10, "l[0],l[1],g.l[0][1],g.l[1]"),
        ("anon.cpp", TYPEDEF_NAMED, 13, "a.p[1],a.p[0][1],b.p[1],b.p[0][1],pr[0],pr[1]"),
    ):
        (tmp_path / name).write_text(text)
        probe = ["probe", str(tmp_path / name), "--at", str(line), "--json", "--no-save"]
        assert main([*probe, "--quiet", "--capture", capture]) == 0
        rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
        if name == "rows.c":
            assert rows["parr[0]"]["type"] == rows["parr[00]"]["type"] == "int[3]"
            assert rows["parr[0][1]"]["value"] == "2"
            assert rows["pp[0].y"]["value"] == rows["pp.y"]["value"] == "2"
            assert "read parr[0][1] for its element" in rows["parr[1]"]["error"]
            assert "read parr[0][-1]" in rows["parr[-1]"]["error"]
            assert "index the array where it is declared" in rows["pp[1]"]["error"]
        elif name == "rows.cpp":
            assert rows["vp[0]"]["value"] == "size=3" and rows["vp[0][1]"]["value"] == "8"
            assert "read vp[0][1] for its element" in rows["vp[1]"]["error"]
        elif name == "names.cpp":
            # Review 4: a member typedef's last name (`Table::Rows`, `Graph::vector`)
            # resolved `lib::Rows` (lldb-dap leaves out the inline namespace) and plain
            # `std::vector<int>`.
            # Round 5: `lib::v1::Rows` is known by the name lldb-dap shows, `lib::Rows`.
            assert "not read" in rows["r[1]"]["error"], rows
            assert rows["r[0][1]"].get("value") in (None, "2"), rows["r[0][1]"]
            assert rows["rf[0]"].get("value") in (None, "7"), rows["rf[0]"]
            assert rows["rf[1]"].get("value") in (None, "8"), rows["rf[1]"]
            assert rows["v[0]"]["value"] == "7" and rows["v[1]"]["value"] == "8", rows
            assert rows["h.r[0]"]["value"] == "size=3" and rows["g.v[0][1]"]["value"] == "2"
            assert "read g.v[0][1]" in rows["g.v[1]"]["error"]
        elif name == "anon.cpp":
            # Review 6: an unnamed struct a typedef names shows by that name (`Anon::P`),
            # so its member typedef took another `Anon::P`'s kind, or none.
            # Review 7: one named twice (`} Anon, Other;`) shows by the first name.
            for base in ("a.p", "b.p"):
                assert "not read" in rows[f"{base}[1]"]["error"], rows
                assert rows[f"{base}[0][1]"].get("value") in (None, "2"), rows
            assert rows["pr[0]"].get("value") in (None, "7"), rows
            assert rows["pr[1]"].get("value") in (None, "8"), rows
        elif name in ("inline.cpp", "local.cpp"):
            # Review 5: lldb-dap leaves out inline namespaces and a local class's
            # function (`Ref`, `Node::Link`), so another entity shown alike (a function's
            # own `Ref`, the file's `Node`) gave its kind to these.
            expected = {"rf[0]": "7", "rf[1]": "8", "r[0][1]": "2", "l[0]": "7", "l[1]": "8"}
            expected["g.l[0][1]"] = "2"
            for selector, row in rows.items():
                assert row.get("value") in (None, expected.get(selector)), row
        else:
            # Review of #117: a typedef'd pointer shows by the typedef's name alone, as a
            # typedef'd reference does; the build's debug information tells them apart.
            assert rows["rp[0][1]"]["value"] == "2" and "read rp[0][1]" in rows["rp[1]"]["error"]
            assert rows["rr[1]"]["value"] == "2" and rows["vr[1]"]["value"] == "8"
            # One fetch of the pointee's first page serves both segments.
            assert rows["bigp[0][450]"]["value"] == "4"
            # A vector's own length is not one its type states.
            assert "read its elements from bigp[0][0]" in rows["bigp[-1]"]["error"]
            assert rows["ep[0]"]["value"] == "size=0"
            assert "np is NULL" in rows["np[0]"]["error"]
            assert "cannot read what garbage points at" in rows["garbage[0]"]["error"]
            # Review 2: the typedef behind lldb-dap's `const RowPtr` resolves; a template's
            # member typedef does not, and is refused rather than indexed either way; a
            # scalar pointer still reads; a pointer declared below the stop is not read.
            assert rows["crp[0][1]"]["value"] == "2"
            assert "not read" in rows["tp[1]"]["error"]
            assert rows["dp[0]"]["value"] == "7"
            assert "has no value here" in rows["later[0]"]["error"]
            # Review 3: a `*` inside template arguments is not the type's own; a class's
            # member typedef resolves by its last name where all agree (or is refused);
            # an unnamed namespace's typedef by its plain name.
            assert "not read" in rows["pc[1]"]["error"]
            assert rows["vref[0]"].get("value") in (None, "3"), rows["vref[0]"]
            assert rows["avp[0][1]"]["value"] == "8"
    main(["session", "start", str(tmp_path / "rows.c"), "--at", "8"])
    capsys.readouterr()
    try:
        assert main(["session", "get", "parr", "--json"]) == 0
        listed = json.loads(capsys.readouterr().out)
        rows = listed["rows"] if isinstance(listed, dict) else listed
        selectors = [child.get("selector") for child in rows[0]["children"]]
        assert selectors == ["parr[0][0]", "parr[0][1]", "parr[0][2]"], rows
    finally:
        main(["session", "close"])
        capsys.readouterr()


MAPS = """#include <array>
#include <cstdio>
#include <deque>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
using Index = std::map<int, int>;
typedef std::set<int> IntSet;
struct Holder { std::map<int, int> m; Index im; int n; };
static int total(const Index &ref, std::map<int, int> *mp, const IntSet *isp, Index *ip) {
    return ref.at(1) + mp->at(2) + (int)isp->size() + ip->at(1); // IN_TOTAL
}
int main() {
    std::map<int, int> m{{2, 20}, {1, 10}, {5, 50}};
    std::unordered_map<int, int> um{{2, 20}, {1, 10}, {5, 50}};
    std::set<int> s{3, 1, 2};
    std::multimap<int, int> mm{{1, 10}, {1, 11}, {2, 20}};
    std::multiset<int> ms{2, 1, 1};
    std::unordered_set<int> us{3, 1, 2};
    std::map<std::string, int> sm{{"b", 2}, {"a", 1}};
    std::map<double, int> dm{{1.5, 2}, {0.5, 1}};
    std::map<int, int, std::greater<int>> gm;
    for (int i = 0; i < 40; i++) gm[i * 3] = i;
    Index im{{2, 20}, {1, 10}};
    IntSet is{3, 1, 2};
    Holder h{{{2, 20}, {1, 10}}, {{7, 70}}, 3};
    std::vector<int> v{7, 8, 9};
    std::array<int, 3> a{7, 8, 9};
    std::deque<int> d{7, 8, 9};
    std::vector<std::map<int, int>> vm{{{4, 40}}};
    int n = total(im, &m, &is, &im) + (int)um.size() + (int)s.size() + (int)mm.size() + (int)ms.size();
    n += (int)us.size() + sm["a"] + dm[0.5] + gm[3] + (int)is.count(1) + h.m[1] + h.im[7];
    n += v[1] + a[1] + d[1] + vm[0][4];
    std::printf("%d\\n", n); // OBSERVE
    return 0;
}
"""


def _session_rows(main, capsys, *selectors):
    main(["session", "get", *selectors, "--json"])
    shown = json.loads(capsys.readouterr().out)
    return {row["selector"]: row for row in (shown["rows"] if isinstance(shown, dict) else shown)}


@contextlib.contextmanager
def _maps_session(tmp_path, capsys, monkeypatch, marker="// OBSERVE"):
    """A session on MAPS stopped at ``marker``; yields `session get` as {selector: row}."""
    from peeky.cli import main

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "maps.cpp"
    source.write_text(MAPS)
    line = next(i for i, text in enumerate(MAPS.splitlines(), 1) if marker in text)
    assert main(["session", "start", str(source), "--at", str(line)]) == 0
    capsys.readouterr()
    try:
        yield lambda *selectors: _session_rows(main, capsys, *selectors)
    finally:
        main(["session", "close"])
        capsys.readouterr()


def test_a_cpp_maps_index_is_the_value_for_that_key(tmp_path, native_config, capsys, monkeypatch):
    # #119: LLDB lists a map's entries as [0], [1], ... in iteration order, so `m[1]` read
    # the second entry ({2, 20}; `m[1].second` was 20) where C++'s m[1] is 10. Now [K] is
    # the value for key K, and [#N] is the Nth entry.
    with _maps_session(tmp_path, capsys, monkeypatch) as get:
        rows = get(
            *("m[1]", "m[2]", "m[5]", "m[3]", "m[#1].first", "m[#1].second", "m[1].second"),
            *("um[1]", "um[5]", "um[7]", 'sm["a"]', "sm['b']", "sm[0]", "dm[0]"),
            *("gm[3]", "gm[117]", "gm[4]", "im[1]", "h.m[1]", "h.im[7]", "vm[0][4]"),
        )
        values = {selector: row.get("value") for selector, row in rows.items()}
        errors = {selector: row.get("error", "") for selector, row in rows.items()}
        assert values["m[1]"] == "10" and values["m[2]"] == "20" and values["m[5]"] == "50"
        assert rows["m[1]"]["type"] == "int"
        assert "m has no key 3 (keys: 1, 2, 5)" in errors["m[3]"]
        assert values["m[#1].first"] == "2" and values["m[#1].second"] == "20"
        assert "m[1] is the value for that key, as in C++" in errors["m[1].second"]
        assert values["um[1]"] == "10" and values["um[5]"] == "50"
        assert "um has no key 7" in errors["um[7]"]
        assert values['sm["a"]'] == "1" and values["sm['b']"] == "2"
        assert 'sm\'s keys are strings; select one as sm["key"]' in errors["sm[0]"]
        assert "dm's keys are double, which Peeky does not compare" in errors["dm[0]"]
        # std::greater: bisected in descending order.
        assert values["gm[3]"] == "1" and values["gm[117]"] == "39"
        assert "descending (std::greater) order" in errors["gm[4]"]
        # A typedef, a struct's members, a map inside a vector.
        assert values["im[1]"] == "10" and values["h.m[1]"] == "10" and values["h.im[7]"] == "70"
        assert values["vm[0][4]"] == "40"
    # A typedef'd map behind a reference, and a pointer to a map.
    with _maps_session(tmp_path, capsys, monkeypatch, "// IN_TOTAL") as get:
        rows = get("ref[1]", "ref[#1].first", "mp[0][2]", "mp[1]", "mp[#0]")
        rows.update(get("isp[0][1]", "isp[0][#1]", "ip[0][1]", "ip[1]"))
        assert rows["ref[1]"]["value"] == "10" and rows["ref[#1].first"]["value"] == "2"
        assert rows["mp[0][2]"]["value"] == "20"
        assert "read mp[0][1], its value for key 1" in rows["mp[1]"]["error"]
        assert "mp[0][#0] is that entry" in rows["mp[#0]"]["error"]
        # Pointers to typedefs: the class behind what they point at.
        assert "is a C++ set" in rows["isp[0][1]"]["error"]
        assert rows["isp[0][#1]"]["value"] == "2"
        assert rows["ip[0][1]"]["value"] == "10"
        assert "read ip[0][1], its value for key 1" in rows["ip[1]"]["error"]


def test_a_cpp_set_or_multimap_index_is_refused(tmp_path, native_config, capsys, monkeypatch):
    # #119: `s[1]` read a set's second element and `mm[1]` a multimap's second entry,
    # where C++ has no [] for either. Now they are refused with the [#N] that reads one.
    with _maps_session(tmp_path, capsys, monkeypatch) as get:
        rows = get("s[1]", "s[#1]", "mm[1]", "mm[#1].second", "ms[0]", "ms[#2]", "us[0]")
        rows.update(get("us[#0]", "is[1]", "is[#0]"))
        values = {selector: row.get("value") for selector, row in rows.items()}
        errors = {selector: row.get("error", "") for selector, row in rows.items()}
        assert "is a C++ set, which has no [] in C++" in errors["s[1]"]
        assert "select an element by position with s[#N]" in errors["s[1]"]
        assert values["s[#1]"] == "2"
        assert "C++ multimap, which has no [] in C++" in errors["mm[1]"]
        # Equal keys keep their insertion order, in both standard libraries.
        assert values["mm[#1].second"] == "11"
        assert "is a C++ set" in errors["ms[0]"] and values["ms[#2]"] == "2"
        assert "is a C++ set" in errors["us[0]"] and values["us[#0]"] in ("1", "2", "3")
        # A typedef'd set, by the class the build's debug information gives.
        assert "is a C++ set" in errors["is[1]"] and values["is[#0]"] == "1"


def test_a_cpp_map_or_set_listing_never_labels_an_entry_by_position(
    tmp_path, native_config, capsys, monkeypatch
):
    # #119: `session get m` listed m[0], m[1], m[2], each read by position.
    with _maps_session(tmp_path, capsys, monkeypatch) as get:
        listed = get("m", "um", "s", "mm", "sm", "dm", "im", "is")
        selectors = {
            name: [(child.get("selector"), child.get("value")) for child in row["children"]]
            for name, row in listed.items()
        }
        assert selectors["m"] == [("m[1]", "10"), ("m[2]", "20"), ("m[5]", "50")]
        assert sorted(selectors["um"]) == [("um[1]", "10"), ("um[2]", "20"), ("um[5]", "50")]
        assert selectors["sm"] == [('sm["a"]', "1"), ('sm["b"]', "2")]
        assert selectors["im"] == [("im[1]", "10"), ("im[2]", "20")]
        assert "m[K] is the value for key K, as in C++" in listed["m"]["note"]
        for name in ("s", "mm", "dm", "is"):
            assert [selector for selector, _ in selectors[name]] == [
                f"{name}[#{i}]" for i in range(len(selectors[name]))
            ]
        assert "s[#N] is its Nth element in iteration order" in listed["s"]["note"]
        assert "dm is a C++ map whose keys Peeky does not compare" in listed["dm"]["note"]
        # Each listed selector reads the value its row shows.
        for name in ("m", "um", "sm", "s"):
            shown = dict(selectors[name])
            assert {s: r.get("value") for s, r in get(*shown).items()} == shown
    with _maps_session(tmp_path, capsys, monkeypatch, "// IN_TOTAL") as get:
        listed = get("ref", "mp", "isp", "ip")
        assert [c["selector"] for c in listed["ref"]["children"]] == ["ref[1]", "ref[2]"]
        assert [c["selector"] for c in listed["isp"]["children"]] == [
            "isp[0][#0]",
            "isp[0][#1]",
            "isp[0][#2]",
        ]
        assert [c["selector"] for c in listed["ip"]["children"]] == ["ip[0][1]", "ip[0][2]"]
        assert [c["selector"] for c in listed["mp"]["children"]] == [
            "mp[0][1]",
            "mp[0][2]",
            "mp[0][5]",
        ]


def test_cpp_sequences_index_as_before(tmp_path, native_config, capsys, monkeypatch):
    # #119 changed maps and sets only: vectors, deques and arrays index by position.
    with _maps_session(tmp_path, capsys, monkeypatch) as get:
        rows = get("v[1]", "d[1]", "v[-1]", "v", "a")
        assert rows["v[1]"]["value"] == rows["d[1]"]["value"] == "8"
        assert "use a non-negative index" in rows["v[-1]"]["error"]
        assert [c["selector"] for c in rows["v"]["children"]] == ["v[0]", "v[1]", "v[2]"]
        assert "note" not in rows["v"]
        # std::array: LLDB's element listing, or its one raw member, indexes as before.
        children = rows["a"]["children"]
        if [c["name"] for c in children] == ["[0]", "[1]", "[2]"]:
            assert children[1]["selector"] == "a[1]" and children[1]["value"] == "8"
        else:
            (raw,) = children
            selector = f"{raw['selector']}[1]"
            assert get(selector)[selector]["value"] == "8"


TYPEDEF_NAMED = """#include <cstdio>
#include <vector>
typedef std::vector<int> &P;
typedef struct { typedef int (*P)[3]; P p; } Anon, Other;
namespace x { struct Anon { typedef int P; P n; }; struct Other { typedef int P; }; }
int main() {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    std::vector<int> v{7, 8, 9};
    Anon a{rows};
    Other b{rows};
    x::Anon xa{4};
    P pr = v;
    int n = a.p[1][0] + b.p[1][0] + xa.n + pr[1];
    std::printf("%d\\n", n);
    return 0;
}
"""

INLINE_NAMES = """#include <cstdio>
#include <vector>
inline namespace v1 {
    using Ref = std::vector<int> &;
    typedef int (*Rows)[3];
}
static int helper(std::vector<int> *vs, int (*rows)[3]) {
    typedef std::vector<int> *Ref;
    typedef int Rows;
    Ref p = vs;
    Rows k = rows[0][0];
    return (*p)[0] + k;
}
int main() {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    std::vector<int> v{7, 8, 9};
    std::vector<int> vs[2] = {v, v};
    Ref rf = v;
    Rows r = rows;
    int n = rf[1] + r[1][0] + helper(vs, rows);
    std::printf("%d\\n", n);
    return 0;
}
"""

LOCAL_CLASS = """#include <cstdio>
#include <vector>
struct Node { typedef int (*Link)[3]; Link l; };
int main() {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    std::vector<int> v{7, 8, 9};
    Node g{rows};
    struct Node { using Link = std::vector<int> &; };
    Node::Link l = v;
    int n = g.l[1][0] + l[1];
    std::printf("%d\\n", n);
    return 0;
}
"""

SAME_NAMES = """#include <cstdio>
#include <vector>
#include <map>
namespace lib { inline namespace v1 {
    typedef int (*Rows)[3];
    using Ref = std::vector<int> &;
    typedef std::vector<int> *VecP;
} }
struct Table { typedef int Rows; Rows n; };
struct Holder { using Ref = std::vector<int> *; Ref r; };
struct Graph { typedef int (*vector)[3]; typedef std::map<int, int> *VecP; vector v; VecP m; };
int main() {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    std::vector<int> v{7, 8, 9};
    std::vector<int> vs[2] = {v, v};
    std::map<int, int> mm{{1, 10}};
    lib::Rows r = rows;
    lib::Ref rf = v;
    lib::VecP vp = vs;
    Table t{3};
    Holder h{&v};
    Graph g{rows, &mm};
    int k = r[1][0] + rf[1] + (*vp)[0] + t.n + (*h.r)[0] + g.v[0][0] + (int)g.m->size() + v[1];
    std::printf("%d\\n", k);
    return 0;
}
"""

ROWS = """#include <stdio.h>
struct pt { int x, y; };
int main(void) {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    int (*parr)[3] = rows;
    struct pt pts[2] = {{1, 2}, {3, 4}};
    struct pt *pp = pts;
    int n = parr[1][2] + pp[1].y;
    printf("%d\\n", n);
    return 0;
}
"""

VECTOR = """#include <cstdio>
#include <vector>
int main() {
    std::vector<int> v{7, 8, 9};
    std::vector<int> *vp = &v;
    int n = (*vp)[1];
    std::printf("%d\\n", n);
    return 0;
}
"""

ALIASES = """#include <cstdio>
#include <vector>
typedef int (*RowPtr)[3];
typedef int (&RowRef)[3];
using VecRef = std::vector<int> &;
struct pt { int x, y; };
namespace { typedef std::vector<int> *AVP; }
int main() {
    int rows[2][3] = {{1, 2, 3}, {4, 5, 6}};
    std::vector<int> v{7, 8, 9}, big(500, 4), empty;
    std::vector<int> *bigp = &big, *ep = &empty;
    std::vector<std::vector<int>> vv{{1, 2}, {3, 4}};
    RowPtr rp = rows;
    const RowPtr crp = rows;
    RowRef rr = rows[0];
    VecRef vr = v;
    std::vector<std::vector<int>>::pointer tp = vv.data();
    std::vector<int>::pointer dp = v.data();
    std::vector<std::vector<const char *>> vcs{{"a", "b"}, {"c", "d"}};
    std::vector<std::vector<const char *>>::pointer pc = vcs.data();
    std::vector<std::vector<int>>::reference vref = vv[1];
    AVP avp = &v;
    pt *np = nullptr;
    int (*garbage)[3] = (int (*)[3])0x10;
    int n = rp[1][0] + crp[1][0] + rr[1] + vr[1] + (int)bigp->size() + (int)ep->size();
    int (*later)[3] = rows;
    n += tp[1][0] + dp[0] + later[1][0] + (np ? 1 : 0) + (garbage ? 1 : 0);
    n += (int)pc[1].size() + vref[0] + (*avp)[0];
    std::printf("%d\\n", n);
    return 0;
}
"""


AVG = """#include <stdio.h>
struct node { int *count; };
typedef int *intp;
static int sum(const int *values, int *count, struct node n, int **pp,
               int *const cp, const int *restrict rp, intp tp) {
    int total = values[0] + *count + *n.count + **pp + *cp + *rp + *tp;
    return total;
}
int main(void) {
    int values[3] = {-3, 1, 7};
    int c = 100, d = 1;
    int *p = &d;
    struct node n = {&d};
    printf("%d\\n", sum(values, &c, n, &p, &c, &d, &c));
    return 0;
}
"""
