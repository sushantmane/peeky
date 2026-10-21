"""The host's process table, to end every process a debug session started (#143).

A debugger adapter starts the debuggee itself, often in a process group of its own
(debugpy's launcher, debugserver), so ending the adapter's group does not reach it, and
an adapter that never answers `disconnect` cannot end it either. Peeky ends what the
session started (``Family``): the processes it listed as descended from the adapter or
from the debuggee the adapter announced while they ran, still the same processes, and
every member of their process groups.
"""

import os
import subprocess
import threading
import time
from pathlib import Path

# Bound at import: a caller (or a test) replacing subprocess.Popen must not change what
# lists the processes Peeky ends.
_POPEN = subprocess.Popen


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


def table():
    """{pid: (ppid, pgid, start, state)} for every process, or None if unreadable.

    ``start`` tells one process from a later one that reused its pid (Linux: its start
    in clock ticks since boot; macOS: ``ps`` lstart). ``state`` starts with "Z" for a
    zombie, which has already exited (and "X" for a process being removed, on Linux).
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
                found[int(entry.name)] = (int(fields[1]), int(fields[2]), fields[19], fields[0])
            except (OSError, ValueError, IndexError):
                continue
        return found
    listing = _ps("-A", "-o", "pid=,ppid=,pgid=,stat=,lstart=")
    if listing is None:
        return None
    found = {}
    for line in listing.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and all(part.isdigit() for part in parts[:3]):
            found[int(parts[0])] = (int(parts[1]), int(parts[2]), parts[4].strip(), parts[3])
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
    """The processes one debug session started, as far as Peeky can tell (#143).

    ``record`` lists, from a process table, the adapter's descendants (while it runs)
    and those of every one listed before that is still the same process (same start):
    its kin. A process whose parent dies is reparented and leaves that tree, but never
    its process group, so every member of a kin process's group is kin too: a group
    lives on while any member does, and its id is never reused meanwhile. The debuggee
    the adapter announced counts as itself only, until a listing shows it descended
    from the adapter. Only processes started after the last listing, in a group of
    their own, whose parent is gone, cannot be told from anyone else's.
    """

    def __init__(self, root):
        # The adapter: its own process group, which is ended apart.
        self.root = root
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
            roots = same | ({self.root} if root_running else set())
            found = {pid for pid in descendants(table, roots) | same if alive(table[pid])}
            found.discard(self.root)
            groups = self._groups(found, table)
            found |= {
                pid
                for pid, entry in table.items()
                if entry[1] in groups and alive(entry) and pid != self.root
            }
            for pid in found:
                self.kin[pid] = table[pid][2]
            self.listed = time.monotonic()
            found |= {pid for pid, start in self.announced.items() if _same(table, pid, start)}
            return {pid: table[pid] for pid in found}, groups

    def groups(self, pids, table):
        """The process groups of those ``pids`` that are still the kin listed."""
        with self.lock:
            return self._groups(
                (pid for pid in pids if _same(table, pid, self.kin.get(pid))), table
            )


def _same(table, pid, start):
    entry = table.get(pid)
    return start is not None and alive(entry) and entry[2] == start
