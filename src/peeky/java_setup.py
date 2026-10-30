"""Build the Java adapter bundle that Peeky debugs Java with (`peeky setup-java`).

Setup downloads Microsoft java-debug's source and five runtime jars, each pinned by URL and
SHA-256 in java_adapter/dependencies.lock.json, patches the source, and compiles it with
Peeky's launcher (java_adapter/PeekyJavaAdapter.java) in clean staging. Downloads (in size
and in total time), tool output and tool time are bounded. The finished bundle is moved into
place in one step, with its provenance in adapter.json and the upstream licence and notices
in notices/. A failure removes only its own staging. Setup never publishes anything.

Each bundle records its recipe: the text of Peeky's patches, the launcher, the lock, the
notices, the compiler flags, and the JDK (java's and javac's paths and version texts), with
the recipe's fingerprint. A bundle is used, and kept as up to date, only while its recipe is
the one this Peeky builds with that JDK (recipe_mismatch).

Without an output directory, setup installs Peeky's own bundle (install). Each build is an
immutable version directory in $PEEKY_STATE_DIR/java-adapter, and a pointer file there names
the one in use. Builds are serialized by a lock, and the pointer is switched atomically once
a version is complete. Every directory on the way is only yours (private_directory). Java
targets whose configuration names no bundle use the version the pointer names (installed),
resolved once when they start.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from peeky import state_home
from peeky.processes import CREDENTIAL_ENV

# The launcher, the lock and the notices ship in the package (package data).
SOURCES = Path(__file__).resolve().with_name("java_adapter")
LOCK = SOURCES / "dependencies.lock.json"
LAUNCHER = SOURCES / "PeekyJavaAdapter.java"
NOTICES = SOURCES / "NOTICES.md"
MAX_DOWNLOAD = 25 * 1024 * 1024
# All of one setup's downloads together.
DOWNLOAD_SECONDS = 300
# Peeky's own bundle: $PEEKY_STATE_DIR/java-adapter/<version>, named by `current`.
DEFAULT_NAME = "java-adapter"
POINTER = "current"
LOCK_NAME = ".lock"
LOCK_SECONDS = 600
STAGING_PREFIX = ".peeky-java-"
VERSION = re.compile(r"[0-9a-f]{20}-[0-9a-f]{8}\Z")
# What a build does beyond its inputs; bump it when the steps change.
RECIPE_SCHEMA = 1
JAVAC_FLAGS = ("--release", "17")
TOOL_KEYS = ("java", "java_version", "javac", "javac_version")
UNUSABLE = "Peeky's own Java adapter can't be used: "

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
    """Where `peeky setup-java` installs Peeky's own bundle, and where Java targets look for
    it when their configuration names none: `java-adapter` in Peeky's state directory."""
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


def patch_texts() -> dict:
    """Each patch's text, as a hash: the file it changes, the code it replaces and with what."""
    return {
        name: hashlib.sha256(json.dumps(list(spec)).encode()).hexdigest()
        for name, spec in sorted(PATCHES.items())
    }


def acquire(item: dict, deadline: float | None = None) -> bytes:
    """The pinned download ``item``, read in chunks: at most MAX_DOWNLOAD bytes, by
    ``deadline`` (time.monotonic(); by default DOWNLOAD_SECONDS from now), and with its
    pinned SHA-256."""
    if deadline is None:
        deadline = time.monotonic() + DOWNLOAD_SECONDS
    url, chunks, size = item["url"], [], 0
    late = TimeoutError(f"downloads exceeded their {DOWNLOAD_SECONDS} second deadline: {url}")
    if (remaining := deadline - time.monotonic()) <= 0:
        raise late
    with urllib.request.urlopen(url, timeout=min(30, max(remaining, 1))) as response:
        while chunk := response.read1(64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError(
                    f"download checksum/size mismatch: {url} is over {MAX_DOWNLOAD} bytes"
                )
            chunks.append(chunk)
            if time.monotonic() >= deadline:
                raise late
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"download checksum/size mismatch: {url}")
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


def toolchain(java: str, javac: str) -> dict:
    """The JDK a bundle is built with and runs on: java's and javac's paths and the version
    texts they print now."""
    with ThreadPoolExecutor(2) as pool:
        java_version, javac_version = pool.map(run, ([java, "-version"], [javac, "-version"]))
    return {
        "java": java,
        "java_version": java_version,
        "javac": javac,
        "javac_version": javac_version,
    }


# toolchain() results recipe_mismatch reused in this process: see _toolchain_now.
_TOOLCHAINS = {}


def _toolchain_now(java: str, javac: str) -> dict:
    """toolchain(), reused in this process while both tools' files (their resolved paths
    and stat identities) and JAVA_HOME are unchanged, as peeky.java reuses a verified
    bundle; a bundle verified before it settles (peeky.digest) asks them only once."""

    def identity(path):
        try:
            info = os.stat(path)
        except OSError:
            return None
        return os.path.realpath(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    tools = (identity(java), identity(javac))
    if None in tools or os.environ.get("PEEKY_NO_DIGEST_CACHE") == "1":
        return toolchain(java, javac)
    key = (java, javac, *tools, os.environ.get("JAVA_HOME"))
    if key not in _TOOLCHAINS:
        _TOOLCHAINS[key] = toolchain(java, javac)
    return _TOOLCHAINS[key]


def recipe(tools: dict) -> dict:
    """Everything a bundle is built from with the JDK ``tools`` (toolchain): equal recipes
    build equal bundles."""
    lock = json.loads(LOCK.read_text())
    return {
        "schema": RECIPE_SCHEMA,
        "javac_flags": list(JAVAC_FLAGS),
        "source_commit": lock["source"]["commit"],
        "dependency_lock_sha256": digest(LOCK),
        "launcher_sha256": digest(LAUNCHER),
        "notices_sha256": digest(NOTICES),
        "patches": patch_texts(),
        **{key: tools[key] for key in TOOL_KEYS},
    }


def fingerprint(recorded: dict) -> str:
    return hashlib.sha256(
        json.dumps(recorded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _difference(recorded, wanted: dict) -> str:
    """Why the recipe a bundle records isn't ``wanted``, in a few words."""
    if not isinstance(recorded, dict):
        return "it records no build recipe: an earlier Peeky built it"
    if set(recorded) != set(wanted) or any(
        recorded[key] != wanted[key] for key in wanted if key not in TOOL_KEYS
    ):
        return "it was built by another version of Peeky"
    return "it was built with another JDK, or the JDK changed since"


def recipe_mismatch(manifest: dict) -> str | None:
    """Why a bundle's manifest doesn't record the recipe this Peeky builds with the JDK it
    names, as that JDK is now; None when it does. The worker checks this before it runs a
    bundle, as setup does before it keeps one."""
    recorded = manifest.get("recipe")
    if not isinstance(recorded, dict):
        return _difference(None, {})
    if manifest.get("fingerprint") != fingerprint(recorded):
        return "its recipe doesn't match its fingerprint"
    if recorded.get("java") != (manifest.get("command") or [None])[0]:
        return "its recipe names another java than its command"
    try:
        tools = _toolchain_now(str(recorded["java"]), str(recorded["javac"]))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return f"its JDK can't be run now: {str(exc)[:200]}"
    wanted = recipe(tools)
    if fingerprint(wanted) != manifest["fingerprint"]:
        return _difference(recorded, wanted)
    return None


def read_manifest(bundle: Path) -> dict:
    """A bundle's adapter.json, which must be a JSON object (ValueError otherwise)."""
    path = bundle / "adapter.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("its adapter.json is a link or over 1 MiB")
    try:
        manifest = json.loads(path.read_text())
    except ValueError as exc:
        raise ValueError(f"its adapter.json is not JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise ValueError("its adapter.json is not a JSON object")
    return manifest


def _jdk() -> tuple[str, str]:
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        raise ValueError("JDK required: java and javac (JDK 17 or later) must be on PATH")
    return java, javac


def setup(output: Path, *, private: bool = False, tools: dict | None = None) -> dict:
    """Build a bundle at ``output``, which must not exist, and return its manifest
    (adapter.json). ``private`` makes the bundle's directory owner-only (0700); ``tools``
    is the JDK to build with (toolchain), by default the one on PATH."""
    if os.path.lexists(output):
        raise ValueError(f"{output} already exists; choose a fresh directory")
    # Resolved like the worker resolves bundle paths (e.g. /tmp is /private/tmp on
    # macOS); an unresolved path would never match the adapter command check.
    output.absolute().parent.mkdir(parents=True, exist_ok=True)
    output = output.absolute().parent.resolve(strict=True) / output.name
    java, javac = (tools["java"], tools["javac"]) if tools else _jdk()
    try:
        return _build(output, tools or toolchain(java, javac), private)
    except Exception as exc:
        # A ValueError: no session is involved, so none is reported closed.
        raise ValueError(f"Java adapter setup failed, and nothing was installed: {exc}") from exc


def _build(output: Path, tools: dict, private: bool) -> dict:
    java, javac = tools["java"], tools["javac"]
    wanted = recipe(tools)
    lock = json.loads(LOCK.read_text())
    started = time.monotonic()
    deadline = started + DOWNLOAD_SECONDS
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=output.parent) as scratch:
        stage = Path(scratch)
        source = stage / "source"
        source.mkdir()
        archive_bytes = io.BytesIO(acquire(lock["source"], deadline))
        with tarfile.open(fileobj=archive_bytes, mode="r:gz") as archive:
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
            (lib / item["url"].rsplit("/", 1)[-1]).write_bytes(acquire(item, deadline))
        sources = sorted((upstream / "com.microsoft.java.debug.core/src/main/java").rglob("*.java"))
        if not sources:
            raise ValueError("core sources missing")
        argfile = stage / "sources.txt"
        argfile.write_text("\n".join('"' + str(p) + '"' for p in sources) + "\n")
        run(
            [
                javac,
                *JAVAC_FLAGS,
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
                *JAVAC_FLAGS,
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
            "patches": {
                name: {**applied, "text_sha256": patch_texts()[name]}
                for name, applied in patches.items()
            },
            "dependency_lock_sha256": wanted["dependency_lock_sha256"],
            "launcher_sha256": wanted["launcher_sha256"],
            "java_version": tools["java_version"],
            "javac_version": tools["javac_version"],
            "recipe": wanted,
            "fingerprint": fingerprint(wanted),
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
        bundle.rename(output)
    return manifest


def private_directory(path: Path, *, fix: bool = False) -> Path:
    """``path``, checked as a directory only you can use, as Peeky's own bundle must be in:
    a real directory (not a link) that you own, mode 0700 (made so with ``fix``), under no
    directory another user can write (peeky.build.writable_by_others). Anyone who could
    write there could supply a bundle, whose adapter.json vouches only for itself."""
    from peeky.build import writable_by_others

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(
            f"{UNUSABLE}{path} is a link, a file or another user's, and Peeky runs it only from "
            "directories only you can use. Move it aside and run `peeky setup-java`, or set "
            "PEEKY_STATE_DIR to a directory of yours"
        )
    if info.st_mode & 0o077:
        if not fix:
            raise ValueError(
                f"{UNUSABLE}{path} has mode {stat.S_IMODE(info.st_mode):o}, open to other "
                f"users. Run `chmod 700 {path}`, or `peeky setup-java`, which does"
            )
        os.chmod(path, 0o700)
    if (shared := writable_by_others(path)) is not None:
        raise ValueError(
            f"{UNUSABLE}{shared} is writable by other users, who could replace the adapter "
            f"in {path}. If it is yours, run `chmod go-w {shared}`; or set PEEKY_STATE_DIR to "
            "a directory only you can write"
        )
    return path


def installed(container: Path | None = None) -> Path | None:
    """The version of Peeky's own bundle that its pointer names, or None where none is
    installed. The state directory, the bundle's directory and the version must be only
    yours (private_directory), and the pointer a file of yours, mode 0600, naming a
    version: anything else is refused (ValueError)."""
    container = default_bundle() if container is None else container
    if not os.path.lexists(container.parent) or not os.path.lexists(container):
        return None
    private_directory(container.parent)
    private_directory(container)
    pointer = container / POINTER
    try:
        descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        info = None
    else:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            name = stream.read(100).decode("ascii", "replace").strip()
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or not VERSION.fullmatch(name)
    ):
        raise ValueError(
            f"{UNUSABLE}{pointer} must be a file of yours, mode 0600, naming a version. Run "
            "`peeky setup-java` to replace it"
        )
    version = container / name
    if not os.path.lexists(version):
        raise ValueError(
            f"{UNUSABLE}{pointer} names {name}, which is gone. Run `peeky setup-java` to "
            "build it again"
        )
    return private_directory(version)


@contextlib.contextmanager
def _locked(container: Path, progress=None):
    """Hold the owner-only lock that serializes setups of Peeky's own bundle."""
    import fcntl

    path = container / LOCK_NAME
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise ValueError(f"{UNUSABLE}{path} is a link. Move it aside") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"{UNUSABLE}{path} is not a file of yours. Move it aside")
        deadline = time.monotonic() + LOCK_SECONDS
        waited = False
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "Java adapter setup failed, and nothing was installed: another "
                        f"`peeky setup-java` held {path} for {LOCK_SECONDS} s"
                    ) from None
                if not waited and progress is not None:
                    progress("Waiting for another `peeky setup-java` to finish.")
                waited = True
                time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


def _point(container: Path, name: str) -> None:
    """Switch the pointer to version ``name`` in one step: a new file of yours, synced, then
    renamed over the old one."""
    draft = container / f".{POINTER}.{secrets.token_hex(4)}"
    descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(name + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(draft, container / POINTER)
    directory = os.open(container, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _sweep(container: Path, keep: set[str] | None) -> None:
    """Under the lock: remove what setups that died left (staging, pointer drafts), and with
    ``keep``, every version but those."""
    for entry in container.iterdir():
        name = entry.name
        leftover = name.startswith((STAGING_PREFIX, f".{POINTER}."))
        if not (leftover or (keep is not None and VERSION.fullmatch(name) and name not in keep)):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def current(bundle: Path, wanted: dict) -> str | None:
    """Why ``bundle`` is not what this Peeky builds with the recipe ``wanted`` (recipe), or
    None when it is: the same fingerprint, and it verifies as the worker verifies it before
    every use, with the `java` it names."""
    from peeky.java import _tool, _verify_bundle

    try:
        resolved = bundle.resolve(strict=True)
        manifest = read_manifest(resolved)
    except (OSError, ValueError) as exc:
        return f"it does not verify: {str(exc)[:200] or type(exc).__name__}"
    if manifest.get("fingerprint") != fingerprint(wanted):
        return _difference(manifest.get("recipe"), wanted)
    try:
        _verify_bundle(resolved, _tool(wanted["java"]))
    except (OSError, ValueError) as exc:
        if "predates Peeky's java-debug fixes" in str(exc):
            return "it predates Peeky's java-debug fixes"
        return f"it does not verify: {str(exc)[:200] or type(exc).__name__}"
    return None


def install(*, force: bool = False, progress=None) -> dict:
    """Install Peeky's own bundle: keep the version the pointer names while it is current
    (unless ``force``), else build a new version and switch the pointer to it once it is
    complete. The version before stays, for sessions still using it; older ones go.
    ``progress`` takes one-line messages."""
    container = default_bundle()
    container.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_directory(container.parent, fix=True)
    if not os.path.lexists(container):
        container.mkdir(mode=0o700)
    private_directory(container, fix=True)
    java, javac = _jdk()
    with _locked(container, progress):
        _sweep(container, keep=None)
        try:
            tools = toolchain(java, javac)
        except Exception as exc:
            raise ValueError(
                f"Java adapter setup failed, and nothing was installed: {exc}"
            ) from exc
        wanted = recipe(tools)
        try:
            previous, why = installed(container), None
        except ValueError as exc:
            previous, why = None, str(exc).removeprefix(UNUSABLE).split(". ")[0]
        if previous is not None and not force:
            why = current(previous, wanted)
            if why is None:
                manifest = read_manifest(previous)
                return {"bundle": str(previous.resolve()), "built": False, "adapter": manifest}
        version = container.resolve() / f"{fingerprint(wanted)[:20]}-{secrets.token_hex(4)}"
        if progress is not None:
            action = "Rebuilding" if force else "Replacing" if why else "Building"
            progress(
                f"{action} the Java adapter in {container}{f' ({why})' if why else ''}: "
                "downloading java-debug and its jars (pinned by SHA-256) and compiling them "
                "can take a minute."
            )
        manifest = setup(version, private=True, tools=tools)
        try:
            _point(container, version.name)
        except OSError as exc:
            raise ValueError(
                f"Java adapter setup failed, and nothing was installed: {exc}"
            ) from exc
        _sweep(container, keep={version.name} | ({previous.name} if previous else set()))
        return {"bundle": str(version), "built": True, "adapter": manifest}


def main() -> None:
    """scripts/setup_java_adapter.py: build a bundle at --output and print its manifest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(setup(args.output), indent=2))
