"""A Rust workspace's build's stop (a threading.Event): ``stopping`` makes it the stop of
every process the build runs (agent.execute) and of every step of Peeky's own work in it
(``check_stop``); ``listed`` and ``walked`` read directories lazily, a step per entry.
Apart from agent, so that the build's modules (rust_build, rust_workspace) need nothing
else of it."""

import contextlib
import contextvars
import os
from pathlib import Path

# The ``stop`` of every ``execute`` in this context that names none (``stopping``).
_STOP = contextvars.ContextVar("peeky_execute_stop", default=None)


@contextlib.contextmanager
def stopping(event):
    """Within it, ``execute`` takes ``event`` for its ``stop`` where given none: a Rust
    workspace's build, whose Cargo commands a worker's shutdown stops. Such an ``execute``
    that the event ends raises Stopped."""
    token = _STOP.set(event)
    try:
        yield
    finally:
        _STOP.reset(token)


def check_stop():
    """Raise Stopped (started False) where ``stopping``'s event is set: a loop of Peeky's
    own work in a stopped build (walking, hashing, listing) ends at its next entry, as its
    next process would never start."""
    event = _STOP.get()
    if event is not None and event.is_set():
        raise Stopped(("peeky",), started=False)


class Stopped(RuntimeError):
    """An ``execute`` that ``stopping``'s event ended: its process group was sent SIGKILL
    and its leader reaped, or, with ``started`` False, it started none."""

    def __init__(self, command, started):
        self.started = started
        super().__init__(
            f"{Path(str(command[0])).name} was stopped" + ("" if started else " before it started")
        )


def listed(directory, limit, refusal, *, stoppable=True):
    """``directory``'s entries (os.DirEntry), sorted by name once read. They are read lazily:
    each checks the build's stop (``check_stop``, unless not ``stoppable``) and spends
    ``limit`` (a list of one count, shared across calls) as scandir yields it, so no more
    than the limit is ever held; past it, ValueError(``refusal``)."""
    found = []
    with os.scandir(directory) as listing:
        for entry in listing:
            if stoppable:
                check_stop()
            limit[0] -= 1
            if limit[0] < 0:
                raise ValueError(refusal)
            found.append(entry)
    return sorted(found, key=lambda each: each.name)


def walked(top, limit, refusal):
    """Every entry under ``top`` (os.DirEntry), depth first in name order, never through a
    link, each directory read by ``listed`` (``limit`` and ``refusal`` its)."""
    pending = [Path(top)]
    while pending:
        for entry in listed(pending.pop(), limit, refusal):
            yield entry
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
