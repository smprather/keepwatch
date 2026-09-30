"""Advisory file locks (flock) shared by the service and manual commands."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusy(Exception):
    """Raised by a non-blocking hold_lock when another process holds the lock."""


@contextmanager
def hold_lock(
    path: Path,
    *,
    blocking: bool = True,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Hold an exclusive flock on path for the duration of the block.

    Non-blocking: raise LockBusy if it is held. Blocking: call on_wait (if given)
    once before waiting.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not blocking:
                raise LockBusy(f"{path} is held by another keepwatch process") from None
            if on_wait is not None:
                on_wait()
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)
