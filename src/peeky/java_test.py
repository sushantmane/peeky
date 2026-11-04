"""`--test` for Java (ADR 0009, section 2): Peeky runs the project's test command with JDWP's
agent added to the JVM that runs the tests, never the build tool's own; finds that JVM by
the exact options it added; and, once the debugger has attached, proves the connection is
between that JVM and the debugger Peeky started. Without that proof nothing is read.

- Maven: `-Dmaven.surefire.debug` (and failsafe's), which surefire adds to the JVMs it forks.
- Gradle: an init script that adds the agent to every `Test` task's `jvmArgs` once the
  build is configured.
- `java` and `javac` (`-J`): the agent among the JVM's first options.

The agent listens on a loopback port the JVM picks (`address=127.0.0.1:0`) and suspends it
before any code runs, so no JVM left over from an earlier run, nor port 5005, plays a part.
A random nonce, `-Dpeeky.launch=<nonce>`, names this run's JVMs. The port is read from the
operating system (`/proc` on Linux, `lsof` on macOS), not from the build's output.

The build is Peeky's: its own process group, its output in Peeky's state directory. The probe
ends FINISH_MIN_S before the deadline; the build may end on its own until the deadline, when
Peeky stops it. Where the build runs several test JVMs at once, nothing is attached: the line
may run in one Peeky isn't attached to.
"""

import ctypes
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from peeky.errors import Refused
from peeky.textlines import split_lines

AGENT = "-agentlib:jdwp=transport=dt_socket,server=y,suspend=y,address=127.0.0.1:0"
MARK = "-Dpeeky.launch="
# With --test, a probe's --timeout covers the build too.
TIMEOUT_S = 300.0
# The least of the deadline a probe keeps after the JVM is found.
PROBE_MIN_S = 20.0
# The part of the deadline kept for the build to end on its own after the probe: the probe
# ends this much before it, and the build is stopped at it.
FINISH_MIN_S = 15.0
# How long Peeky watches for more test JVMs once one waits: a build that runs several at
# once (Gradle's maxParallelForks, Maven's forkCount) starts them together.
SETTLE_S = 2.0
# How long a stopped build gets after SIGTERM, then after SIGKILL (_stop).
STOP_GRACE_S = 5.0
POLL_S = 0.2
TAIL_LINES = 30
TAIL_BYTES = 16 * 1024
_NOTHING_RAN = "Nothing ran. Change the test command as the message says."
# Options of `java` that take the next word as their value.
_JAVA_VALUE_OPTIONS = frozenset(
    {
        "-cp",
        "-classpath",
        "--class-path",
        "-p",
        "--module-path",
        "--upgrade-module-path",
        "--add-modules",
        "--add-opens",
        "--add-exports",
        "--add-reads",
        "--patch-module",
        "--limit-modules",
        "--enable-native-access",
    }
)
_DEBUG_OPTION = re.compile(r"-(?:J)?(?:-?agentlib:jdwp|Xrunjdwp|Xdebug|agentpath:.*jdwp)")
# Only the JVM arguments: each Test task keeps its own maxParallelForks and forkEvery.
GRADLE_INIT = """// Peeky's (ADR 0009): JDWP's agent for this run's test JVMs only.
gradle.taskGraph.whenReady { graph ->
    graph.allTasks.findAll { it instanceof Test }.each { task ->
        task.jvmArgs({agent}, {mark})
    }
}
"""


def _refused(code, message):
    """A refusal before the test command started: nothing ran."""
    return Refused(code, message, _NOTHING_RAN if code == "UNSUPPORTED" else None)


def _stopped(message, next_step=""):
    """A refusal after the test command started: nothing was read, and Peeky stops the
    test (finish)."""
    return Refused(
        "UNSUPPORTED", message, f"Nothing was read; Peeky stopped the test. {next_step}".strip()
    )


@dataclass
class Launch:
    """One run of a test command under Peeky: its ``tool``, the ``command`` Peeky runs (the
    test command with the agent added), and this run's ``nonce``."""

    tool: str
    words: tuple[str, ...]
    command: list[str]
    nonce: str
    scratch: Path
    log: Path
    process: subprocess.Popen | None = None
    jvm: tuple[int, int] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mark(self):
        return MARK + self.nonce


def prepare(words, scratch):
    """The Launch of ``words``; refused before anything runs where Peeky can't add its agent
    to the test JVM alone."""
    words = tuple(words)
    name = Path(words[0]).name
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        raise _refused("UNSUPPORTED", "--test for Java runs on Linux and macOS only")
    nonce = secrets.token_hex(16)
    scratch = Path(scratch)
    log = scratch / f"java-test-{nonce[:12]}.log"
    mark = MARK + nonce
    if found := next((w for w in words[1:] if _DEBUG_OPTION.match(w)), None):
        raise _refused(
            "INVALID_REQUEST",
            f"The test command already has a debug option ({found}): drop it. Peeky adds its own",
        )
    if name in ("mvn", "mvnw"):
        for word in words[1:]:
            if re.match(r"-D(maven\.surefire\.debug|maven\.failsafe\.debug)\b", word):
                raise _refused(
                    "INVALID_REQUEST",
                    f"The test command sets {word.partition('=')[0][2:]}: drop it. Peeky "
                    "sets it to its own agent",
                )
            if re.fullmatch(r"-DforkCount=0+(\.0*)?C?", word):
                raise _refused(
                    "UNSUPPORTED",
                    f"{word} runs the tests in Maven's own JVM, which Peeky doesn't attach "
                    "to: drop it, so that surefire forks the test JVM",
                )
        debug = f"{AGENT} {mark}"
        command = [
            *words,
            f"-Dmaven.surefire.debug={debug}",
            f"-Dmaven.failsafe.debug={debug}",
        ]
        return Launch("maven", words, command, nonce, scratch, log)
    if name in ("gradle", "gradlew"):
        if "--debug-jvm" in words:
            raise _refused(
                "INVALID_REQUEST",
                "The test command has --debug-jvm: drop it. Peeky adds its own agent, on a "
                "port the JVM picks",
            )
        init = scratch / f"peeky-init-{nonce[:12]}.gradle"
        text = GRADLE_INIT.replace("{agent}", _groovy(AGENT)).replace("{mark}", _groovy(mark))
        fd = os.open(init, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        return Launch("gradle", words, [words[0], "-I", str(init), *words[1:]], nonce, scratch, log)
    if name == "java":
        return Launch("java", words, [words[0], AGENT, mark, *words[1:]], nonce, scratch, log)
    if name == "javac":
        return Launch(
            "javac", words, [words[0], f"-J{AGENT}", f"-J{mark}", *words[1:]], nonce, scratch, log
        )
    raise _refused(
        "UNSUPPORTED",
        f"--test can't add Peeky's debug agent to {name}'s test JVM: give the `java` command "
        "the test runs, or Maven or Gradle's",
    )


def _groovy(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def start(launch, cwd):
    """Start the launch's command in its own process group, its output in its log."""
    fd = os.open(launch.log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        launch.process = subprocess.Popen(
            launch.command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise _refused("INVALID_REQUEST", f"Peeky can't start {launch.words[0]}: {exc}") from None
    finally:
        os.close(fd)
    return launch.process


def jvm_options(argv):
    """A `java` command line's JVM options: the words before its main class, `-jar`,
    `-m` or a source file, and after its program name."""
    options, words = [], list(argv[1:])
    while words:
        word = words.pop(0)
        if word in ("-jar", "-m", "--module") or not word.startswith("-"):
            break
        options.append(word)
        if word in _JAVA_VALUE_OPTIONS and words:
            options.append(words.pop(0))
    return options


def is_test_jvm(argv, mark):
    """Whether ``argv`` is a JVM given both of Peeky's words as JVM options: the agent and
    this run's mark. The build tool's own JVM may hold the mark inside one of its program's
    arguments (`-Dmaven.surefire.debug=…`), never as an option of its own. `javac` runs its
    JVM in its own process, and takes them as `-J` words."""
    name = Path(argv[0]).name if argv else ""
    if name == "javac":
        return f"-J{AGENT}" in argv[1:] and f"-J{mark}" in argv[1:]
    if name != "java":
        return False
    options = jvm_options(argv)
    return AGENT in options and mark in options


def processes():
    """{pid: argv} of this user's processes, as far as they can be read."""
    if sys.platform.startswith("linux"):
        found = {}
        uid = os.getuid()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                raw = Path(entry.path, "cmdline").read_bytes()
            except OSError:
                continue
            found[int(entry.name)] = [w.decode(errors="replace") for w in raw.split(b"\0")[:-1]]
        return found
    if sys.platform == "darwin":
        listed = subprocess.run(
            ["ps", "-axo", "pid=,uid="], capture_output=True, text=True, timeout=30
        ).stdout
        uid = os.getuid()
        found = {}
        for line in split_lines(listed):
            parts = line.split()
            if len(parts) == 2 and parts[1] == str(uid):
                argv = _darwin_argv(int(parts[0]))
                if argv:
                    found[int(parts[0])] = argv
        return found
    raise _stopped("--test for Java runs on Linux and macOS only")


def _darwin_argv(pid):
    """A process's argv on macOS (sysctl KERN_PROCARGS2), or None."""
    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    data = buffer.raw[: size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    # The executable's path, then NUL padding, then argc words.
    end = rest.find(b"\0")
    rest = rest[end:].lstrip(b"\0")
    words = rest.split(b"\0")[:argc]
    return [w.decode(errors="replace") for w in words] if len(words) == argc else None


def parents():
    """{pid: parent pid} of the system's processes."""
    if sys.platform.startswith("linux"):
        found = {}
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    stat = Path(entry.path, "stat").read_text()
                except OSError:
                    continue
                found[int(entry.name)] = int(stat.rpartition(")")[2].split()[1])
        return found
    listed = subprocess.run(
        ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=30
    ).stdout
    return {
        int(a): int(b) for a, b in (line.split() for line in split_lines(listed) if line.strip())
    }


def descendants(pid):
    """``pid`` and every process under it."""
    tree = parents()
    found, grown = {pid}, True
    while grown:
        more = {child for child, parent in tree.items() if parent in found} - found
        found |= more
        grown = bool(more)
    return found


def _linux_sockets():
    """[(inode, state, local (ip, port), remote (ip, port))] of TCP sockets on 127.0.0.1."""
    found = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = split_lines(Path(name).read_text())[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, remote = _address(parts[1]), _address(parts[2])
            if local[0] == "127.0.0.1":
                found.append((int(parts[9]), parts[3], local, remote))
    return found


def _address(text):
    """(ip, port) of a /proc/net/tcp address: the address in 32-bit words, each in the
    host's byte order; an IPv6 socket's IPv4-mapped address (`::ffff:127.0.0.1`, as a
    JVM's dual-stack socket has) as its IPv4 address."""
    ip, _, port = text.partition(":")
    raw = bytes.fromhex(ip)
    raw = b"".join(
        int.from_bytes(raw[at : at + 4], sys.byteorder).to_bytes(4, "big")
        for at in range(0, len(raw), 4)
    )
    if len(raw) == 16 and raw[:12] == b"\0" * 10 + b"\xff\xff":
        raw = raw[12:]
    shown = ".".join(str(b) for b in raw) if len(raw) == 4 else raw.hex()
    return shown, int(port, 16)


def _linux_inodes(pid):
    inodes = set()
    try:
        for entry in os.scandir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(entry.path)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(int(target[8:-1]))
    except OSError:
        pass
    return inodes


def listening(pid):
    """The loopback TCP ports ``pid`` listens on."""
    if sys.platform.startswith("linux"):
        mine = _linux_inodes(pid)
        return sorted(
            {
                local[1]
                for inode, state, local, _ in _linux_sockets()
                if inode in mine and state == "0A"
            }
        )
    return sorted({local[1] for _, local, _ in _lsof(["-a", "-p", str(pid), "-sTCP:LISTEN"])})


def _lsof(selection):
    """[(pid, local (ip, port), remote (ip, port) or None)] of the TCP sockets ``selection``
    names, on 127.0.0.1 (macOS)."""
    try:
        listed = subprocess.run(
            ["lsof", "-nP", "-iTCP@127.0.0.1", *selection, "-Fpn"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found, pid = [], None
    for line in split_lines(listed):
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            local, _, remote = line[1:].partition("->")
            ends = [end.rpartition(":") for end in (local, remote) if end]
            if ends and ends[0][0] == "127.0.0.1":
                found.append(
                    (
                        pid,
                        (ends[0][0], int(ends[0][2])),
                        (ends[1][0], int(ends[1][2])) if len(ends) > 1 else None,
                    )
                )
    return found


def connections(port):
    """[(pid, local port, remote port)] of the established loopback connections at
    ``port``, either end."""
    if sys.platform.startswith("linux"):
        sockets = [
            (inode, local[1], remote[1])
            for inode, state, local, remote in _linux_sockets()
            if state == "01" and port in (local[1], remote[1]) and remote[0] == "127.0.0.1"
        ]
        found = []
        for pid in _owners({inode for inode, _, _ in sockets}):
            mine = _linux_inodes(pid)
            found += [(pid, local, remote) for inode, local, remote in sockets if inode in mine]
        return found
    return [
        (pid, local[1], remote[1])
        for pid, local, remote in _lsof([f"-iTCP:{port}", "-sTCP:ESTABLISHED"])
        if remote is not None
    ]


def _owners(inodes):
    """The pids of this user's processes holding any of ``inodes`` (Linux)."""
    owners = []
    for pid in processes():
        if _linux_inodes(pid) & inodes:
            owners.append(pid)
    return owners


def find_jvm(launch, deadline, progress=None):
    """(pid, port) of the first test JVM of ``launch`` that waits on one loopback port; None
    if the build ended first. TimeoutError at the deadline. Refused for a test JVM listening
    on several ports, and where the build runs more than one test JVM at once."""
    said = False
    while True:
        jvms = sorted(pid for pid, argv in processes().items() if is_test_jvm(argv, launch.mark))
        for pid in jvms:
            ports = listening(pid)
            if len(ports) == 1:
                _one_at_a_time(launch, pid, min(deadline, time.monotonic() + SETTLE_S))
                launch.jvm = (pid, ports[0])
                return launch.jvm
            if len(ports) > 1:
                raise _stopped(
                    f"The test JVM (pid {pid}) listens on {len(ports)} loopback ports "
                    f"({', '.join(map(str, ports))}), so Peeky can't tell which is its debug "
                    "agent's: nothing was attached",
                    "Start the test waiting for a debugger on one port, and attach with a "
                    "config (docs/lang/java.md).",
                )
        if launch.process.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"No test JVM with Peeky's debug agent started within the probe's time: "
                f"{launch.tool} was still running. Raise --timeout, or add --background"
            )
        if not said and progress is not None and jvms:
            progress("the test JVM started; waiting for its debug agent")
            said = True
        time.sleep(POLL_S)


def _one_at_a_time(launch, first, until):
    """Refused where the build runs another test JVM beside ``first`` until ``until``:
    Peeky attaches to one, and the line may run in another, which would wait for a debugger
    that never comes. One that starts later, after the first, is finish's to note."""
    while True:
        others = sorted(
            pid
            for pid, argv in processes().items()
            if pid != first and is_test_jvm(argv, launch.mark)
        )
        if others:
            select = {
                "gradle": "Select one test class (--tests NAME), or give the test task "
                "maxParallelForks = 1",
                "maven": "Select one test class (`-Dtest=NAME` for Surefire or `-Dit.test=NAME` "
                "for Failsafe), or configure that test plugin with one fork",
            }.get(launch.tool, "Give a command that starts one JVM")
            raise _stopped(
                f"{launch.tool} started {len(others) + 1} test JVMs at once (pids {first}, "
                f"{', '.join(map(str, others[:4]))}), and Peeky attaches to one: the line may "
                "run in another, so nothing was attached",
                select + ".",
            )
        if time.monotonic() >= until or launch.process.poll() is not None:
            return
        time.sleep(POLL_S)


def check_connected(pid, port):
    """Refused unless the JVM ``pid`` holds an established connection at ``port`` whose
    other end is a process under this one: the debugger Peeky started."""
    found = connections(port)
    jvm = [(local, remote) for owner, local, remote in found if owner == pid and local == port]
    ours = descendants(os.getpid())
    debugger = [
        (local, remote) for owner, local, remote in found if owner in ours and remote == port
    ]
    if any(remote == local for _, remote in jvm for local, _ in debugger):
        return
    raise Refused(
        "UNSUPPORTED",
        f"Peeky can't prove that its debugger attached to the test JVM it started (pid {pid}, "
        f"port {port}): the JVM holds {len(jvm)} connection(s) at that port, and none ends at "
        "Peeky's debugger. Nothing was read",
        "Nothing was read, and Peeky stopped the test. Run the probe again; if this repeats, "
        "start the test waiting for a debugger and attach with a config (docs/lang/java.md).",
    )


def class_roots(root, program, launch):
    """The class roots that may hold ``program``'s classes, relative to ``root``: the build
    tool's output directories in the file's module, or the command's classpath directories
    (`java`, `javac`), where the file's package directory exists."""
    root = Path(root).resolve()
    source = (root / program).resolve()
    package = _package(source)
    candidates = []
    if launch.tool in ("java", "javac"):
        words = list(launch.words[1:])
        for at, word in enumerate(words[:-1]):
            if word in ("-cp", "-classpath", "--class-path", "-processorpath", "--processor-path"):
                candidates += [Path(part) for part in words[at + 1].split(os.pathsep) if part]
        candidates += [
            Path(part)
            for word in words
            if word.startswith(("--class-path=", "--processor-path="))
            for part in word.partition("=")[2].split(os.pathsep)
            if part
        ]
        candidates = [c if c.is_absolute() else Path.cwd() / c for c in candidates]
    else:
        module = source.parent
        markers = ("pom.xml",) if launch.tool == "maven" else ("build.gradle", "build.gradle.kts")
        while module != root and not any((module / m).is_file() for m in markers):
            module = module.parent
        if launch.tool == "maven":
            candidates = [module / "target/classes", module / "target/test-classes"]
        else:
            candidates = sorted((module / "build/classes").glob("*/*"))
    found = []
    for candidate in candidates:
        directory = candidate.resolve()
        if (
            directory.is_dir()
            and directory.is_relative_to(root)
            and directory.joinpath(*package.split(".")).is_dir()
        ):
            relative = directory.relative_to(root).as_posix()
            if relative not in found:
                found.append(relative)
    if not found:
        raise _stopped(
            f"Peeky found no compiled classes of {program} in the build's output "
            f"({', '.join(sorted({_shown(c, root) for c in candidates})) or 'no classpath'}): "
            "the test JVM started, but its classes aren't where Peeky looks",
            "Attach with a config naming class_roots (docs/lang/java.md).",
        )
    return tuple(found)


def _shown(path, root):
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\s*;", re.M)


def _package(source):
    found = _PACKAGE.search(source.read_text(errors="replace")[:65536])
    return found[1] if found else ""


def finish(launch, deadline, progress=None):
    """Let the build end within ``deadline``, then stop it (_stop: SIGTERM, then SIGKILL
    STOP_GRACE_S later, each reaped within STOP_GRACE_S), and any other of this run's test
    JVMs still waiting; what the test did, for the result."""
    process = launch.process
    attached, port = launch.jvm or (None, None)
    others, unattached = set(), False
    while process is not None and process.poll() is None and time.monotonic() < deadline:
        waiting = {
            pid: listening(pid)
            for pid, argv in processes().items()
            if is_test_jvm(argv, launch.mark)
        }
        if attached is None:
            # Nothing was attached (the JVM came too late, or was refused): any test JVM
            # waiting waits for a debugger that won't come.
            if any(waiting.values()):
                unattached = True
                break
        elif port in waiting.get(attached, ()):
            # Still on the port it first listened on: no debugger ever connected (after a
            # detach, JDWP listens again on a new port, the JVM running).
            unattached = True
            break
        elif others := {pid for pid, ports in waiting.items() if pid != attached and ports}:
            break
        time.sleep(POLL_S)
    stopped = None
    if process is not None and process.poll() is None:
        stopped = (
            "Peeky attached to no test JVM, and one waited for a debugger"
            if unattached and attached is None
            else "Peeky didn't attach to its test JVM, which waited for a debugger"
            if unattached
            else "it started another test JVM, which waited for a debugger"
            if others
            else "the probe's time ran out"
        )
        if progress is not None:
            progress(f"stopping {launch.tool}: {stopped}")
        _stop(process)
    for pid, argv in processes().items():
        # Its test JVMs outside the process group, as a Gradle daemon's are.
        if is_test_jvm(argv, launch.mark):
            _kill(pid)
    if others:
        launch.notes.append(
            f"{launch.tool} started {len(others) + 1} test JVMs; Peeky attached to the first "
            "and stopped the build at the next, so the line may run in a JVM Peeky didn't "
            "watch: select one test class"
        )
    _remove(launch)
    return {
        "command": shlex.join(launch.words),
        "kind": "java",
        "tool": launch.tool,
        **({"jvm": {"pid": launch.jvm[0], "port": launch.jvm[1]}} if launch.jvm else {}),
        "exit_code": None if stopped else process.returncode if process else None,
        **({"stopped": stopped} if stopped else {}),
        "log": str(launch.log),
        "output_tail": tail(launch.log),
    }


def _stop(process):
    for sent in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sent)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=STOP_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _remove(launch):
    for path in launch.scratch.glob(f"peeky-init-{launch.nonce[:12]}.gradle"):
        path.unlink(missing_ok=True)


def tail(path, lines=TAIL_LINES):
    """The last ``lines`` lines of a log (at most TAIL_BYTES of it)."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            text = stream.read().decode(errors="replace")
    except OSError:
        return ""
    return "\n".join(split_lines(text)[-lines:])
