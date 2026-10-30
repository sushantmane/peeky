"""The host's process table, to end every process a debug session started.

A debugger adapter starts the debuggee itself, often in a process group of its own
(debugpy's launcher, debugserver), so ending the adapter's group does not reach it, and
an adapter that never answers `disconnect` cannot end it either. Peeky ends what the
session started (``Family``): the processes it listed as descended from the adapter or
from the debuggee the adapter announced while they ran, still the same processes, every
process still in the adapter's own session, and every member of their process
groups.
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from peeky.textlines import split_lines

# Bound at import: a caller (or a test) replacing subprocess.Popen must not change what
# lists the processes Peeky ends.
_POPEN = subprocess.Popen

# The worker's token, by the name the worker reads (DEBUG_TOKEN) and the name a client
# reads (PEEKY_WORKER_TOKEN). Neither reaches a debugger, a debuggee, a build, a checker,
# a candidate or a model.
CREDENTIAL_ENV = frozenset({"DEBUG_TOKEN", "PEEKY_WORKER_TOKEN"})


def without_credentials(environment):
    """A copy of ``environment`` (a mapping, or key and value pairs) without the worker's
    token under either name."""
    return {key: value for key, value in dict(environment).items() if key not in CREDENTIAL_ENV}


def _ps(*arguments):
    """``ps`` output with the C locale, or None if it cannot run within 5 s."""
    try:
        with _POPEN(
            ["ps", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        ) as process:
            try:
                return process.communicate(timeout=5)[0]
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return None
    except OSError:
        return None


def keep_children_waitable():
    """Undo an inherited ignored SIGCHLD: with it, the kernel reaps every child
    as it exits, behind `Popen`, so a dead adapter's pid, and its process group's and
    session's ids, are free for another's while Peeky still names them. Called first by
    every command that starts adapters (`peeky`, `peeky-worker`, `peeky-probe`,
    `peeky-agent`); only Python's main thread can set it, so a no-op elsewhere."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    except (AttributeError, OSError, ValueError):
        pass


def session_id(pid):
    try:
        return os.getsid(pid)
    except OSError:
        return None


def table():
    """{pid: (ppid, pgid, start, state, sid)} for every process, or None if unreadable.

    ``start`` tells one process from a later one that reused its pid (Linux: its start
    in clock ticks since boot; macOS: ``ps`` lstart). ``state`` starts with "Z" for a
    zombie, which has already exited (and "X" for a process being removed, on Linux).
    ``sid`` is its session (None if unreadable): a process is in a session only by
    inheriting it, so one still in the adapter's session was started from it.
    """
    proc = Path("/proc")
    if (proc / "self" / "stat").is_file():
        found = {}
        for entry in os.scandir(proc):
            if not entry.name.isdigit():
                continue
            try:
                data = (proc / entry.name / "stat").read_text()
                # After "pid (comm) ": state ppid pgrp session ... starttime is field 22.
                fields = data[data.rindex(")") + 2 :].split()
                found[int(entry.name)] = (
                    int(fields[1]),
                    int(fields[2]),
                    fields[19],
                    fields[0],
                    int(fields[3]),
                )
            except (OSError, ValueError, IndexError):
                continue
        return found
    listing = _ps("-A", "-o", "pid=,ppid=,pgid=,stat=,lstart=")
    if listing is None:
        return None
    found = {}
    for line in split_lines(listing):
        parts = line.split(None, 4)
        if len(parts) == 5 and all(part.isdigit() for part in parts[:3]):
            # macOS `ps` gives no session id (`sess` is 0): asked of each process.
            found[int(parts[0])] = (
                int(parts[1]),
                int(parts[2]),
                parts[4].strip(),
                parts[3],
                session_id(int(parts[0])),
            )
    return found or None


def descendants(processes, root):
    """The pids whose parent chain leads to ``root``, a pid or several (roots excluded)."""
    roots = {root} if isinstance(root, int) else set(root)
    children = {}
    for pid, (parent, *_) in processes.items():
        children.setdefault(parent, []).append(pid)
    found, pending = set(), list(roots)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found and child not in roots:
                found.add(child)
                pending.append(child)
    return found


def start_time(pid):
    """``pid``'s start, as ``table`` reports it, or None if it is gone or unreadable."""
    stat = Path("/proc") / str(pid) / "stat"
    if Path("/proc/self/stat").is_file():
        try:
            data = stat.read_text()
            return data[data.rindex(")") + 2 :].split()[19]
        except (OSError, ValueError, IndexError):
            return None
    return (_ps("-o", "lstart=", "-p", str(pid)) or "").strip() or None


def alive(entry):
    """Whether a ``table`` entry is a process still running (not a zombie, not dead)."""
    return entry is not None and not entry[3].startswith(("Z", "X"))


class Family:
    """The processes one debug session started, as far as Peeky can tell.

    ``record`` lists, from a process table, the adapter's descendants (while it runs)
    and those of every one listed before that is still the same process (same start):
    its kin. A process whose parent dies is reparented and leaves that tree, but never
    its process group, so every member of a kin process's group is kin too: a group
    lives on while any member does, and its id is never reused meanwhile. The debuggee
    the adapter announced counts with its process group (it may have left the
    adapter's session: `setsid`), while it is the same process (its start), but its
    other descendants count only once a listing shows them descended from the adapter
    or from a kin process.

    ``session`` is the adapter's own session, when it leads one (Peeky starts it so):
    a process can only inherit its session, never join one, so every process still in
    it was started from the adapter, whatever became of its parents, and is kin too
    (a listing that comes only after the adapter died still finds them). Only
    processes started after the last listing, in a session of their own, whose parent
    is gone, cannot be told from anyone else's.
    """

    def __init__(self, root, session=None):
        # The adapter: its own process group, which is ended apart.
        self.root = root
        # Never Peeky's own session: that one holds processes it did not start.
        self.session = session if session not in (None, session_id(0)) else None
        self.kin = {}  # {pid: start}
        self.announced = {}  # {pid: start}
        # When the session's processes were last listed (monotonic), if ever.
        self.listed = None
        self.lock = threading.Lock()

    def announce(self, pid, start):
        with self.lock:
            self.announced.setdefault(pid, start)

    def _groups(self, pids, table):
        # Never the adapter's (ended apart) nor Peeky's own.
        return {table[pid][1] for pid in pids} - {self.root, os.getpgrp()}

    def record(self, table, root_running):
        """Add what ``table`` shows the session started. Returns those still running
        ({pid: entry}) and the process groups found to be the session's."""
        with self.lock:
            same = {pid for pid, start in self.kin.items() if _same(table, pid, start)}
            same |= {
                pid
                for pid, entry in table.items()
                if self.session is not None
                and len(entry) > 4
                and entry[4] == self.session
                and alive(entry)
                and pid != self.root
            }
            roots = same | ({self.root} if root_running else set())
            found = {pid for pid in descendants(table, roots) | same if alive(table[pid])}
            found.discard(self.root)
            announced = {pid for pid, start in self.announced.items() if _same(table, pid, start)}
            groups = self._groups(found | announced, table)
            members = {
                pid
                for pid, entry in table.items()
                if entry[1] in groups and alive(entry) and pid != self.root
            }
            # The announced debuggee itself stays announced only, unless found otherwise.
            for pid in (found | members) - (announced - found):
                self.kin[pid] = table[pid][2]
            self.listed = time.monotonic()
            found |= members | announced
            return {pid: table[pid] for pid in found}, groups

    def holds(self, table, group=None, session=None):
        """Whether a process the session recorded, still the same one, is in ``group``
        (or ``session``): a group's or a session's id is never another's while it has
        members, so the id still names the adapter's own."""
        with self.lock:
            known = {**self.announced, **self.kin}
            return any(
                _same(table, pid, start)
                and (group is None or table[pid][1] == group)
                and (session is None or (len(table[pid]) > 4 and table[pid][4] == session))
                for pid, start in known.items()
            )

    def groups(self, pids, table):
        """The process groups of those ``pids`` that are still the kin listed, or the
        debuggee the adapter announced."""
        with self.lock:
            return self._groups(
                (
                    pid
                    for pid in pids
                    if _same(table, pid, self.kin.get(pid))
                    or _same(table, pid, self.announced.get(pid))
                ),
                table,
            )


def _same(table, pid, start):
    entry = table.get(pid)
    return start is not None and alive(entry) and entry[2] == start
