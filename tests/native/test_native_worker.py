"""Real LLDB C worker conformance; optional locally, required in the native CI lane."""

import hashlib
import json
import os
import re
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


def test_a_pointers_first_element_is_its_pointee(tmp_path, native_config, capsys):
    # Eval 5 dry run: `values[0]` on `const int *values` said only "children: *values".
    from peeky.cli import main

    (tmp_path / "avg.c").write_text(AVG)
    probe = ["probe", str(tmp_path / "avg.c"), "--at", "5", "--json", "--no-save", "--quiet"]
    capture = "values[0],values[1],count[0],n.count[0],n.count[1],pp[0][0],pp[0][1]"
    assert main([*probe, "--capture", capture]) == 0
    rows = {row["selector"]: row for row in json.loads(capsys.readouterr().out)["rows"]}
    values = {name: row.get("value") for name, row in rows.items()}
    assert values["values[0]"] == "-3" and values["count[0]"] == "100"
    assert values["n.count[0]"] == values["pp[0][0]"] == "1"
    # Review of #100: the hint names the whole path, never another variable's.
    for name, path in (("values[1]", "values"), ("n.count[1]", "n.count"), ("pp[0][1]", "pp[0]")):
        assert f"; {path} is a pointer, which carries no length: {path}[0]" in rows[name]["error"]
    main(["session", "start", str(tmp_path / "avg.c"), "--at", "5"])
    capsys.readouterr()
    try:
        assert main(["session", "get", "values", "pp[0]"]) == 0
        shown = capsys.readouterr().out
        assert "values[0]: const int = -3" in shown and "pp[0][0]: int = 1" in shown
    finally:
        main(["session", "close"])
        capsys.readouterr()


AVG = """#include <stdio.h>
struct node { int *count; };
static int sum(const int *values, int *count, struct node n, int **pp) {
    int total = values[0] + *count + *n.count + **pp;
    return total;
}
int main(void) {
    int values[3] = {-3, 1, 7};
    int c = 100, d = 1;
    int *p = &d;
    struct node n = {&d};
    printf("%d\\n", sum(values, &c, n, &p));
    return 0;
}
"""
