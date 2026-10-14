import os
import shutil
import threading
from pathlib import Path

import pytest

from peeky.client import Client
from peeky.native import NativeTarget
from peeky.native_verification import NativeVerification
from peeky.worker import make_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cpp_investigation(tmp_path):
    bundle = os.environ.get("PEEKY_NATIVE_DAP")
    if not bundle:
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    root = tmp_path / "inputs"
    shutil.copytree(ROOT / "examples/cpp", root)
    source = root / "pagination_bug.cpp"
    check = root / "verify_pagination.py"
    target = NativeTarget(
        sources=("pagination_bug.cpp",),
        language="cpp",
        headers=("pagination.hpp",),
        compiler=os.environ.get("PEEKY_NATIVE_CXX", "clang++"),
        adapter=bundle,
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    token = "native-agent-tests-token-123456"
    worker_root = tmp_path / "worker-inputs"
    shutil.copytree(root, worker_root)
    server = make_server(worker_root, token, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_port}", token)
    execution = NativeVerification(target, root)
    observe_line = next(
        i for i, line in enumerate(source.read_text().splitlines(), 1) if "/* OBSERVE */" in line
    )
    yield client, execution, source, check, observe_line, tmp_path / "report"
    server.worker.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
