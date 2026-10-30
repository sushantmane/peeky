"""Build the Java adapter bundle that Peeky debugs Java with (`peeky setup-java`).

Setup downloads Microsoft java-debug's source and five runtime jars, each pinned by URL and
SHA-256 in java_adapter/dependencies.lock.json, patches the source, and compiles it with
Peeky's launcher (java_adapter/PeekyJavaAdapter.java) in clean staging. Downloads, tool
output and tool time are bounded. The finished bundle is moved into place in one step, with
its provenance in adapter.json and the upstream licence and notices in notices/. A failure
removes only its own staging. Setup never publishes anything.

Without an output directory, the bundle goes to Peeky's state directory (default_bundle),
where Java targets whose configuration names no bundle find it.
"""

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

from peeky import state_home
from peeky.processes import CREDENTIAL_ENV

# The launcher, the lock and the notices ship in the package (package data).
SOURCES = Path(__file__).resolve().with_name("java_adapter")
LOCK = SOURCES / "dependencies.lock.json"
LAUNCHER = SOURCES / "PeekyJavaAdapter.java"
NOTICES = SOURCES / "NOTICES.md"
DEFAULT_NAME = "java-adapter"
MAX_DOWNLOAD = 25 * 1024 * 1024

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


def default_bundle() -> Path:
    """Where `peeky setup-java` puts the bundle, and where Java targets look for it when
    their configuration names none: `java-adapter` in Peeky's state directory."""
    return state_home() / DEFAULT_NAME


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


def _replaceable(output: Path) -> bool:
    """Whether ``output`` exists as a directory that setup may replace: a real one (not a
    symlink), owned by this user."""
    if not os.path.lexists(output):
        return False
    info = output.lstat()
    if output.is_symlink() or not output.is_dir() or info.st_uid != os.getuid():
        raise ValueError(
            f"{output} is not a directory you own (a symlink, a file or another user's); "
            "move it aside, or pass --output with a new directory"
        )
    return True


def setup(output: Path, *, replace: bool = False, private: bool = False) -> dict:
    """Build a bundle at ``output`` and return its manifest (adapter.json).

    ``output`` must not exist, unless ``replace``: then an existing directory there is
    swapped for the new bundle once that is complete. ``private`` makes the bundle's
    directory owner-only (0700)."""
    if os.path.lexists(output) and not replace:
        raise ValueError(f"{output} already exists; choose a fresh directory")
    # Resolved like the worker resolves bundle paths (e.g. /tmp is /private/tmp on
    # macOS); an unresolved path would never match the adapter command check.
    output.absolute().parent.mkdir(parents=True, exist_ok=True)
    output = output.absolute().parent.resolve(strict=True) / output.name
    existing = replace and _replaceable(output)
    java = shutil.which("java")
    javac = shutil.which("javac")
    if not java or not javac:
        raise ValueError("JDK required: java and javac (JDK 17 or later) must be on PATH")
    try:
        return _build(output, java, javac, existing, private)
    except Exception as exc:
        # A ValueError: no session is involved, so none is reported closed.
        raise ValueError(f"Java adapter setup failed, and nothing was installed: {exc}") from exc


def _build(output: Path, java: str, javac: str, existing: bool, private: bool) -> dict:
    java_version, javac_version = run([java, "-version"]), run([javac, "-version"])
    lock = json.loads(LOCK.read_text())
    started = time.monotonic()
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
        run(
            [
                javac,
                "--release",
                "17",
                "-cp",
                f"{classes}{os.pathsep}{lib}/*",
                "-d",
                str(classes),
                str(LAUNCHER),
            ]
        )
        notices = bundle / "notices"
        notices.mkdir()
        for name in ("LICENSE.txt", "ThirdPartyNotices.txt"):
            shutil.copyfile(upstream / name, notices / name)
        shutil.copyfile(NOTICES, notices / f"PEEKY-{NOTICES.name}")
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
            "dependency_lock_sha256": digest(LOCK),
            "launcher_sha256": digest(LAUNCHER),
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
        if private:
            bundle.chmod(0o700)
        if existing:
            # The old bundle goes into this staging, which is removed with it.
            output.rename(stage / "old")
            try:
                bundle.rename(output)
            except BaseException:
                (stage / "old").rename(output)
                raise
        else:
            bundle.rename(output)
    return manifest


def current(bundle: Path) -> str | None:
    """Why the bundle at ``bundle`` is not what this Peeky would build now, or None when it
    is: it verifies as the worker verifies it before every use, with the `java` on PATH,
    and it was built from this Peeky's launcher, lock and patches."""
    from peeky.java import _tool, _verify_bundle

    try:
        resolved = bundle.resolve(strict=True)
        _verify_bundle(resolved, _tool("java"))
        manifest = json.loads((resolved / "adapter.json").read_text())
    except (OSError, ValueError) as exc:
        if "predates Peeky's java-debug fixes" in str(exc):
            return "it predates Peeky's java-debug fixes"
        return f"it does not verify: {str(exc)[:200] or type(exc).__name__}"
    lock = json.loads(LOCK.read_text())
    if (
        manifest.get("launcher_sha256") != digest(LAUNCHER)
        or manifest.get("dependency_lock_sha256") != digest(LOCK)
        or manifest.get("source_commit") != lock["source"]["commit"]
        or not set(PATCHES) <= set(manifest.get("patches") or {})
    ):
        return "it was built by another version of Peeky"
    return None


def main() -> None:
    """scripts/setup_java_adapter.py: build a bundle at --output and print its manifest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(setup(args.output), indent=2))
