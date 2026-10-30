#!/usr/bin/env python3
"""Build pinned experimental Java adapter; emit metadata, never publish it."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD = 25 * 1024 * 1024
# peeky.processes.CREDENTIAL_ENV: this script runs without Peeky installed.
CREDENTIAL_ENV = frozenset({"DEBUG_TOKEN", "PEEKY_WORKER_TOKEN"})

# Source patches to the pinned java-debug, applied before compiling and recorded in
# adapter.json (`patches`); peeky.java refuses a bundle without them.
#
# eventhub-log-guard: the "Event Hub" thread, which turns every JDI event into DAP
# stops, steps, thread and exit events, builds a FINE log line for each event even
# with FINE off. The line names the event's thread; for a short-lived thread already
# collected that throws ObjectCollectedException, which nothing catches, so the thread
# dies and no JVM event is processed again (lost stops and exits).
EVENT_HUB = (
    "com.microsoft.java.debug.core/src/main/java/com/microsoft/java/debug/core/EventHub.java"
)
EVENT_HUB_ORIGINAL = """\
                        try {
                            logger.fine("\\nJDI Event: " + event + "\\n");
                        } catch (VMDisconnectedException e) {
                            // do nothing
                        }
"""
EVENT_HUB_PATCHED = """\
                        if (logger.isLoggable(java.util.logging.Level.FINE)) {
                            try {
                                logger.fine("\\nJDI Event: " + event + "\\n");
                            } catch (RuntimeException e) {
                                // ObjectCollectedException (a finished thread), VMDisconnectedException
                            }
                        }
"""
PATCHES = {"eventhub-log-guard": (EVENT_HUB, EVENT_HUB_ORIGINAL, EVENT_HUB_PATCHED)}


def apply_patches(upstream: Path) -> dict:
    """Patch the extracted java-debug tree in place; idempotent, and loud when the text
    to patch is not exactly where it was (the pinned source changed)."""
    applied = {}
    for name, (relative, original, patched) in PATCHES.items():
        path = upstream / relative
        text = path.read_text()
        if text.count(original) == 1 and patched not in text:
            text = text.replace(original, patched)
            path.write_text(text)
        elif not (text.count(patched) == 1 and original not in text):
            raise ValueError(
                f"java-debug patch {name} does not apply to {relative}: the code it changes "
                "was not found exactly once; review the patch against the pinned source"
            )
        applied[name] = {"file": relative, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return applied


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acquire(item: dict) -> bytes:
    with urllib.request.urlopen(item["url"], timeout=30) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"download checksum/size mismatch: {item['url']}")
    return data


def run(command: list[str]) -> str:
    # Pinned compiler inputs still deserve bounded diagnostics and a process deadline.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env={k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV},
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        try:
            while True:
                if os.fstat(output.fileno()).st_size > 64 * 1024:
                    raise RuntimeError("tool output exceeds 64 KiB")
                if time.monotonic() >= deadline:
                    raise TimeoutError("tool exceeded 90 second deadline")
                try:
                    process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if os.fstat(output.fileno()).st_size > 64 * 1024:
                raise RuntimeError("tool output exceeds 64 KiB")
            output.seek(0)
            text = output.read(64 * 1024).decode("utf-8", errors="replace").strip()
            if process.returncode:
                raise RuntimeError(f"command failed: {command[0]}\n{text[-8000:]}")
            return text
        finally:
            # Kill the owned group even if the parent exited, to reap stray tool children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


def setup(output: Path) -> dict:
    if os.path.lexists(output):
        raise ValueError("output already exists; choose a fresh directory")
    # Resolved like the worker resolves bundle paths (e.g. /tmp is /private/tmp on
    # macOS); an unresolved path would never match the adapter command check.
    output.absolute().parent.mkdir(parents=True, exist_ok=True)
    output = output.absolute().parent.resolve(strict=True) / output.name
    java = shutil.which("java")
    javac = shutil.which("javac")
    if not java or not javac:
        raise ValueError("JDK required: java and javac must be on PATH")
    java_version, javac_version = run([java, "-version"]), run([javac, "-version"])
    lock_path = ROOT / "adapters/java/dependencies.lock.json"
    lock = json.loads(lock_path.read_text())
    started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".peeky-java-", dir=output.parent) as scratch:
        stage = Path(scratch)
        source = stage / "source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(acquire(lock["source"])), mode="r:gz") as archive:
            for member in archive.getmembers():
                target = source / member.name
                if not target.resolve().is_relative_to(source.resolve()):
                    raise ValueError("unsafe source archive path")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    if member.size > MAX_DOWNLOAD:
                        raise ValueError("oversized source archive entry")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    assert stream is not None
                    target.write_bytes(stream.read())
                else:
                    raise ValueError("unsupported source archive entry")
        upstream = next(source.iterdir())
        patches = apply_patches(upstream)
        bundle = stage / "bundle"
        lib, classes = bundle / "lib", bundle / "classes"
        lib.mkdir(parents=True)
        classes.mkdir()
        for item in lock["dependencies"]:
            (lib / item["url"].rsplit("/", 1)[-1]).write_bytes(acquire(item))
        sources = sorted((upstream / "com.microsoft.java.debug.core/src/main/java").rglob("*.java"))
        if not sources:
            raise ValueError("core sources missing")
        argfile = stage / "sources.txt"
        argfile.write_text("\n".join('"' + str(p) + '"' for p in sources) + "\n")
        run(
            [
                javac,
                "--release",
                "17",
                "-cp",
                str(lib / "*"),
                "-d",
                str(classes),
                "@" + str(argfile),
            ]
        )
        launcher = ROOT / "adapters/java/PeekyJavaAdapter.java"
        run(
            [
                javac,
                "--release",
                "17",
                "-cp",
                f"{classes}{os.pathsep}{lib}/*",
                "-d",
                str(classes),
                str(launcher),
            ]
        )
        notices = bundle / "notices"
        notices.mkdir()
        for name in ("LICENSE.txt", "ThirdPartyNotices.txt"):
            shutil.copyfile(upstream / name, notices / name)
        shutil.copyfile(ROOT / "adapters/java/NOTICES.md", notices / "PEEKY-NOTICES.md")
        manifest = {
            "schema": 1,
            "experimental": True,
            "command": [
                java,
                "-cp",
                f"{output}/classes{os.pathsep}{output}/lib/*",
                "PeekyJavaAdapter",
            ],
            "source_map_argument": "append absolute prepared source-map JSON path",
            "source_commit": lock["source"]["commit"],
            "patches": patches,
            "dependency_lock_sha256": digest(lock_path),
            "launcher_sha256": digest(launcher),
            "java_version": java_version,
            "javac_version": javac_version,
            "files": {
                str(p.relative_to(bundle)): digest(p)
                for p in sorted(bundle.rglob("*"))
                if p.is_file()
            },
            "setup_seconds": round(time.monotonic() - started, 3),
        }
        (bundle / "adapter.json").write_text(json.dumps(manifest, indent=2) + "\n")
        bundle.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(setup(args.output), indent=2))


if __name__ == "__main__":
    main()
