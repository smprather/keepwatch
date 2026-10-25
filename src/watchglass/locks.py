"""Advisory file locks shared by the service and manual commands (flock on POSIX, msvcrt on Windows)."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from watchglass import platform


class LockBusy(Exception):
    """Raised by a non-blocking hold_lock when another process holds the lock."""


@contextmanager
def probe_lock(path: Path) -> Generator[bool]:
    """Whether an exclusive holder has `path`, asked without taking it: yields True when a service holds it.

    A plain non-blocking exclusive probe would hold the lock it is testing, so two probes overlapping (two
    monitors, or a monitor and `watchglass status`) would each report a service that is not running - and the
    same window could make `watchglass run` refuse to start while something was merely looking.
    """
    if not path.exists():
        yield False
        return
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        yield False
        return
    held = False
    try:
        held = not platform.try_lock_shared(fd)
        yield held
    finally:
        if not held:
            try:
                platform.unlock(fd)  # release a shared lock we took; nothing to release when it was busy
            except OSError:
                pass
        os.close(fd)


@contextmanager
def hold_lock(
    path: Path,
    *,
    blocking: bool = True,
    on_wait: Callable[[], None] | None = None,
) -> Generator[None]:
    """Hold an exclusive lock on path for the duration of the block.

    Non-blocking: raise LockBusy if it is held. Blocking: call on_wait (if given)
    once before waiting.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if not platform.try_lock(fd):
            if not blocking:
                raise LockBusy(f"{path} is held by another watchglass process")
            if on_wait is not None:
                on_wait()
            platform.lock(fd)
        locked = True
        yield
    finally:
        if locked:
            platform.unlock(fd)
        os.close(fd)
