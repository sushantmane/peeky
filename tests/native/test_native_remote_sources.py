"""#249: a header that differs on the worker host is refused before the build, by name."""

import json
import os
import threading

import pytest

from peeky.cli import WORKER_TOKEN_ENV, main

TOKEN = "native-remote-sources-token-123"
MAIN = """#include <stdio.h>
#include "page.h"
int main(void) {
  int start = PAGE * PAGE_SIZE;
  printf("START:%d\\n", start);
  return 0;
}
"""
HEADER = "#define PAGE 1\n#define PAGE_SIZE 3\n"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    if not os.environ.get("PEEKY_NATIVE_DAP"):
        pytest.skip("set PEEKY_NATIVE_DAP and native tool paths to enable native conformance")
    from peeky.native import NativeTarget
    from peeky.worker import make_server

    monkeypatch.setenv("PEEKY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(WORKER_TOKEN_ENV, TOKEN)
    host, local = tmp_path / "host", tmp_path / "local"
    for root in (host, local):
        root.mkdir()
        (root / "main.c").write_text(MAIN)
        (root / "page.h").write_text(HEADER)
    target = NativeTarget(
        sources=("main.c",),
        headers=("page.h",),
        compiler=os.environ.get("PEEKY_NATIVE_CC", "clang"),
        adapter=os.environ["PEEKY_NATIVE_DAP"],
        dwarfdump=os.environ.get("PEEKY_NATIVE_DWARFDUMP", "llvm-dwarfdump"),
        dsymutil=os.environ.get("PEEKY_NATIVE_DSYMUTIL"),
        sysroot=os.environ.get("PEEKY_NATIVE_SYSROOT"),
    )
    server = make_server(host, TOKEN, 0, adapter=target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield host, local, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.worker.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def probe(capsys, local, url):
    argv = ["probe", str(local), "--at", "main.c:5", "--capture", "start", "--worker", url]
    code = main([*argv, "--json", "--quiet", "--no-save"])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.timeout(120)
def test_a_header_that_differs_on_the_worker_host_is_refused_by_name(remote, capsys):
    host, local, url = remote
    code, result = probe(capsys, local, url)
    # The same sources: the worker confirms it compared them, and read what we hold.
    assert code == 0, result.get("error")
    assert [row["value"] for row in result["rows"]] == ["3"] and "START:3" in result["output"]
    (host / "page.h").write_text(HEADER.replace("3", "4"))
    code, result = probe(capsys, local, url)
    assert code == 2 and result["error"]["code"] == "PROVENANCE_CHANGED", result
    assert (
        "for page.h (the copy on the worker host differs from your checkout)"
        in (result["error"]["message"])
    )
    assert result["refused_before_launch"] is True and result["rows"] == []
