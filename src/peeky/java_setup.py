"""Build the Java adapter bundle that Peeky debugs Java with (`peeky setup-java`).

Setup downloads Microsoft java-debug's source and five runtime jars, each pinned by URL and
SHA-256 in java_adapter/dependencies.lock.json, patches the source, and compiles it with
Peeky's launcher (java_adapter/PeekyJavaAdapter.java) in clean staging. Downloads (in size
and in total time), tool output and tool time are bounded. The finished bundle is moved into
place in one step, with its provenance in adapter.json and the upstream licence and notices
in notices/. A failure removes only its own staging. Setup never publishes anything.

Each bundle records its recipe: the text of Peeky's patches, the launcher, the lock, the
notices, the compiler flags (`--release 17`, so any JDK 17 or later runs it), javac's
version, and the file `java` is (its real path and SHA-256), with the recipe's fingerprint.
The worker runs a bundle only if its recipe, but for the JDK, is this Peeky's
(recipe_mismatch), and only on that `java` file; setup keeps one only if the JDK is the
same too (current).

Without an output directory, setup installs Peeky's own bundle (install). Each build is an
immutable version directory in $PEEKY_STATE_DIR/java-adapter, and a pointer file there names
the one in use. Builds are serialized by a lock, and the pointer is switched atomically once
a version is complete. Versions are removed only by `peeky setup-java --prune` (prune).
Every directory on the way is only yours (private_directory). Java targets whose
configuration names no bundle use the version the pointer names (installed), resolved once
when they start.
"""

from __future__ import annotations

import argparse
import contextlib
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
# The recipe's part the JDK gives: setup rebuilds when it differs. The worker never runs the
# JDK to ask it, but checks that the bundle's `java` is still the file it recorded: its real
# path (links followed) and its bytes (peeky.java._verify_bundle).
JDK_KEYS = ("javac_version", "java_path", "java_sha256", "java_jdk")
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


def run(command: list[str], env: dict | None = None) -> str:
    """``command``'s output, stderr too; ``env``, by default this process's environment,
    loses the credentials either way."""
    # Pinned compiler inputs still deserve bounded diagnostics and a process deadline.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env={
                k: v
                for k, v in (os.environ if env is None else env).items()
                if k not in CREDENTIAL_ENV
            },
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


def java_identity(java: str) -> dict:
    """The file ``java`` names: its real path, links followed, and its SHA-256. Where it is
    a JDK's own bin/java (peeky.java.jdk_of), also that JDK's release file and JVM library,
    each by path and SHA-256 (`java_jdk`; else None): the worker checks them all before
    it runs the bundle (peeky.java._same_java)."""
    from peeky.java import jdk_of

    real = os.path.realpath(java)
    jdk, _ = jdk_of(real)
    return {
        "java_path": real,
        "java_sha256": digest(Path(real)),
        "java_jdk": None
        if jdk is None
        else {
            "release": jdk.release,
            "release_sha256": digest(Path(jdk.release)),
            "libjvm": jdk.libjvm,
            "libjvm_sha256": digest(Path(jdk.libjvm)),
        },
    }


def toolchain(java: str, javac: str) -> dict:
    """The JDK setup builds with: java's and javac's paths and the version texts they print
    now (asked afresh each time), and the file java is (java_identity). Where that file is
    not a JDK's own bin/java (peeky.java.jdk_of), such as macOS's /usr/bin/java, also
    the JDK home it says it runs, which only refusals name."""
    from peeky.java import reported_home

    tools = {
        "java": java,
        "java_version": run([java, "-version"]),
        "javac": javac,
        "javac_version": run([javac, "-version"]),
        **java_identity(java),
    }
    if tools["java_jdk"] is None:
        tools["java_home"] = reported_home(java)
    return tools


def recipe(tools: dict) -> dict:
    """Everything a bundle is built from with the JDK ``tools`` (toolchain): equal recipes
    build equal bundles. All but JDK_KEYS is the bundle's own content, which the worker
    checks before it runs one (recipe_mismatch); javac's version only setup checks."""
    lock = json.loads(LOCK.read_text())
    return {
        "schema": RECIPE_SCHEMA,
        "javac_flags": list(JAVAC_FLAGS),
        "source_commit": lock["source"]["commit"],
        "dependency_lock_sha256": digest(LOCK),
        "launcher_sha256": digest(LAUNCHER),
        "notices_sha256": digest(NOTICES),
        "patches": patch_texts(),
        **{key: tools[key] for key in JDK_KEYS},
    }


def fingerprint(recorded: dict) -> str:
    return hashlib.sha256(
        json.dumps(recorded, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _difference(recorded, wanted: dict) -> str | None:
    """Why the recipe a bundle records isn't ``wanted``, in a few words; None if it is."""
    if not isinstance(recorded, dict):
        return "it records no build recipe: an earlier Peeky built it"
    if set(recorded) != set(wanted) or any(
        recorded[key] != wanted[key] for key in wanted if key not in JDK_KEYS
    ):
        return "it was built by another version of Peeky"
    if recorded["javac_version"] != wanted["javac_version"]:
        return "it was built with another javac"
    if any(recorded[key] != wanted[key] for key in JDK_KEYS):
        return "it was built with another java"
    return None


def recipe_mismatch(manifest: dict) -> str | None:
    """Why a bundle's manifest doesn't record what this Peeky builds, or None. Only the
    bundle's own content counts: the recipe must match its fingerprint and, but for the
    JDK it was built with (JDK_KEYS), be this Peeky's (flags, patch texts, launcher, lock,
    notices). The JDK that runs it is not asked: setup compiles for `--release 17`, and a
    JVM that can't run such classes refuses them itself. The worker checks this before it
    runs a bundle, and that its `java` is still the file the recipe records
    (peeky.java._verify_bundle); setup also compares javac's version (current)."""
    recorded = manifest.get("recipe")
    if not isinstance(recorded, dict):
        return _difference(None, {})
    if manifest.get("fingerprint") != fingerprint(recorded):
        return "its recipe doesn't match its fingerprint"
    wanted = recipe({key: recorded.get(key) for key in JDK_KEYS})
    return _difference(recorded, wanted)


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
            **({"java_home": tools["java_home"]} if tools.get("java_home") else {}),
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


def _lstat(path: Path):
    """os.lstat(path), or None where nothing is there; failing to look is a refusal."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ValueError(
            f"{UNUSABLE}{path} can't be read ({exc.strerror or exc}). Make it and the "
            "directories above it yours, or set PEEKY_STATE_DIR to a directory of yours"
        ) from None


def _shared_root(path: Path) -> bool:
    """Whether ``path`` is / or the home directory, whose mode Peeky never changes."""
    resolved = Path(path).resolve()
    return resolved == Path(resolved.anchor) or resolved == Path.home().resolve()


def private_directory(path: Path, *, fix: bool = False) -> Path:
    """``path``, checked as a directory only you can use, as Peeky's own bundle must be in:
    a real directory (not a link) that you own, neither / nor the home directory, mode 0700
    (made so with ``fix``), under no directory another user can write
    (peeky.build.writable_by_others). Anyone who could write there could supply a bundle,
    whose adapter.json vouches only for itself."""
    from peeky.build import writable_by_others

    info = _lstat(path)
    if info is None:
        raise ValueError(f"{UNUSABLE}{path} is missing. Run `peeky setup-java`")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(
            f"{UNUSABLE}{path} is a link, a file or another user's, and Peeky runs it only from "
            "directories only you can use. Move it aside and run `peeky setup-java`, or set "
            "PEEKY_STATE_DIR to a directory of yours"
        )
    if _shared_root(path):
        raise ValueError(
            f"{UNUSABLE}{path} is / or your home directory, which Peeky never takes for its "
            "own, whatever its mode: set PEEKY_STATE_DIR to a directory of Peeky's own"
        )
    if info.st_mode & 0o077:
        if not fix:
            raise ValueError(
                f"{UNUSABLE}{path} has mode {stat.S_IMODE(info.st_mode):o}, open to other "
                f"users. Run `chmod 700 {path}`, or `peeky setup-java`, which does"
            )
        os.chmod(path, 0o700)
    try:
        shared = writable_by_others(path)
    except OSError as exc:
        raise ValueError(
            f"{UNUSABLE}a directory above {path} can't be read ({exc.strerror or exc}). Set "
            "PEEKY_STATE_DIR to a directory of yours"
        ) from None
    if shared is not None:
        raise ValueError(
            f"{UNUSABLE}{shared} is writable by other users, who could replace the adapter "
            f"in {path}. If it is yours, run `chmod go-w {shared}`; or set PEEKY_STATE_DIR to "
            "a directory only you can write"
        )
    return path


def installed(container: Path | None = None) -> Path | None:
    """The version of Peeky's own bundle that its pointer names, or None where none is
    installed. The state directory (checked first, whenever it exists), the bundle's
    directory and the version must be only yours (private_directory), and the pointer a
    file of yours, mode 0600 exactly, naming a version: anything else, a pointer that can't
    be opened or read included, is refused (ValueError)."""
    container = default_bundle() if container is None else container
    if _lstat(container.parent) is None:
        return None
    private_directory(container.parent)
    if _lstat(container) is None:
        return None
    private_directory(container)
    pointer = container / POINTER
    refusal = ValueError(
        f"{UNUSABLE}{pointer} must be a file of yours, mode 0600, naming a version. Run "
        "`peeky setup-java` to replace it"
    )
    try:
        descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        # A link (ELOOP), or one this user can't open.
        raise refusal from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise refusal
            name = stream.read(100).decode("ascii", "replace").strip()
    except OSError:
        raise refusal from None
    if not VERSION.fullmatch(name):
        raise refusal
    version = container / name
    if _lstat(version) is None:
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
        raise ValueError(
            f"{UNUSABLE}{path} is a link, or can't be opened ({exc.strerror or exc}). Move it aside"
        ) from None
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


def _discard(entry: Path) -> None:
    """Remove ``entry``, best-effort: for leftovers no one uses."""
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry, ignore_errors=True)
    else:
        entry.unlink(missing_ok=True)


def _remove(entry: Path) -> str | None:
    """Remove ``entry``; None once it is gone, or why it is still there."""
    try:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"{exc.strerror or exc}: {exc.filename or entry}"
    return "it is still there" if os.path.lexists(entry) else None


def _sweep(container: Path) -> None:
    """Under the lock, so no setup is running: remove, best-effort, what setups that died
    left, their staging and pointer drafts. Versions stay (prune removes them)."""
    for entry in container.iterdir():
        if entry.name.startswith((STAGING_PREFIX, f".{POINTER}.")):
            _discard(entry)


def _versions(container: Path, but: str | None = None) -> list[str]:
    return sorted(
        entry.name
        for entry in container.iterdir()
        if VERSION.fullmatch(entry.name) and entry.name != but
    )


def current(bundle: Path, wanted: dict, java: str) -> str | None:
    """Why ``bundle`` is not what this Peeky builds now, with the recipe ``wanted`` (recipe,
    with javac's version asked afresh), or None when it is: the same fingerprint, and it
    verifies as the worker verifies it before every use, run by ``java``."""
    from peeky.java import ADAPTER_JAVA, _tool, _verify_bundle

    try:
        resolved = bundle.resolve(strict=True)
        manifest = read_manifest(resolved)
    except (OSError, ValueError) as exc:
        return f"it does not verify: {str(exc)[:200] or type(exc).__name__}"
    if manifest.get("fingerprint") != fingerprint(wanted):
        return _difference(manifest.get("recipe"), wanted) or "its fingerprint differs"
    try:
        _verify_bundle(resolved, _tool(java))
    except (OSError, ValueError) as exc:
        if "predates Peeky's java-debug fixes" in str(exc):
            return "it predates Peeky's java-debug fixes"
        if str(exc).startswith(ADAPTER_JAVA):
            return "it was built with another java"
        return f"it does not verify: {str(exc)[:200] or type(exc).__name__}"
    return None


def _own_directories(*, create: bool) -> Path | None:
    """Peeky's own bundle's directory, after the state directory's and its own checks (made
    0700 where they are yours); None where there is none and ``create`` is false."""
    container = default_bundle()
    if _lstat(container.parent) is None:
        if not create:
            return None
        container.parent.mkdir(parents=True, mode=0o700)
    private_directory(container.parent, fix=True)
    if _lstat(container) is None:
        if not create:
            return None
        container.mkdir(mode=0o700)
    return private_directory(container, fix=True)


def install(*, force: bool = False, progress=None) -> dict:
    """Install Peeky's own bundle: keep the version the pointer names while it is current
    (unless ``force``), else build a new version and switch the pointer to it once it is
    complete. Older versions stay, for sessions still using them, until prune. ``progress``
    takes one-line messages."""
    container = _own_directories(create=True)
    java, javac = _jdk()
    with _locked(container, progress):
        _sweep(container)
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
            why = current(previous, wanted, java)
            if why is None:
                return {
                    "bundle": str(previous.resolve()),
                    "built": False,
                    "older": _versions(container, but=previous.name),
                    "adapter": read_manifest(previous),
                }
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
        return {
            "bundle": str(version),
            "built": True,
            "older": _versions(container, but=version.name),
            "adapter": manifest,
        }


def prune(progress=None) -> dict:
    """`peeky setup-java --prune`: remove every version of Peeky's own bundle but the one
    the pointer names, and what setups that died left. A session still using a removed
    version fails its next step (its bundle is gone). A pointer that is refused (installed)
    stops it: then which version is current isn't known. ``pruned`` lists the versions
    that are gone; ``failed`` maps each that is still there to why."""
    container = _own_directories(create=False)
    if container is None:
        return {"bundle": None, "pruned": [], "failed": {}}
    pruned, failed = [], {}
    with _locked(container, progress):
        _sweep(container)
        keep = installed(container)
        for name in _versions(container, but=keep.name if keep else None):
            if (why := _remove(container / name)) is None:
                pruned.append(name)
            else:
                failed[name] = why
    return {"bundle": str(keep.resolve()) if keep else None, "pruned": pruned, "failed": failed}


# The help text's first line; not read from the docstring, which `python -OO` drops.
DESCRIPTION = "Build the Java adapter bundle that Peeky debugs Java with (`peeky setup-java`)."


def main() -> None:
    """scripts/setup_java_adapter.py: build a bundle at --output and print its manifest."""
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(setup(args.output), indent=2))
