"""The host's process table, to end every process a debug session started (#143).

A debugger adapter starts the debuggee itself, often in a process group of its own
(debugpy's launcher, debugserver), so ending the adapter's group does not reach it, and
an adapter that never answers `disconnect` cannot end it either. Peeky ends what the
adapter started: the adapter's descendants, found while their ancestry still leads to
it, and the debuggee it announced, if it is still the same process.
"""

import os
import subprocess
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
    """The pids whose parent chain leads to ``root`` (``root`` excluded)."""
    children = {}
    for pid, (parent, *_) in processes.items():
        children.setdefault(parent, []).append(pid)
    found, pending = set(), [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found and child != root:
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
