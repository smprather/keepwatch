"""Advisory file locks shared by the service and manual commands (flock on POSIX, msvcrt on Windows)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from keepwatch import platform


class LockBusy(Exception):
    """Raised by a non-blocking hold_lock when another process holds the lock."""


@contextmanager
def hold_lock(
    path: Path,
    *,
    blocking: bool = True,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
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
                raise LockBusy(f"{path} is held by another keepwatch process")
            if on_wait is not None:
                on_wait()
            platform.lock(fd)
        locked = True
        yield
    finally:
        if locked:
            platform.unlock(fd)
        os.close(fd)
