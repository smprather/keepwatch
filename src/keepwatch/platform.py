"""Everything that differs between POSIX and Windows. Standard library only.

The rest of keepwatch calls these functions instead of OS-specific APIs.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from pathlib import Path

IS_WINDOWS = os.name == "nt"
APP = "keepwatch"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5


def _absolute(value: str | None) -> Path | None:
    if value and os.path.isabs(value):
        return Path(value)
    return None


def default_app_dirs(env: Mapping[str, str], uid: int | None = None) -> tuple[Path, Path, Path]:
    """keepwatch's config, state and runtime directories when no XDG variable overrides them."""
    if IS_WINDOWS:
        home = Path(env.get("USERPROFILE") or Path.home())
        roaming = _absolute(env.get("APPDATA")) or home / "AppData" / "Roaming"
        local = _absolute(env.get("LOCALAPPDATA")) or home / "AppData" / "Local"
        return roaming / APP, local / APP, local / APP / "run"
    uid = os.getuid() if uid is None else uid
    home = Path(env.get("HOME") or Path.home())
    runtime = (_absolute(env.get("TMPDIR")) or Path("/tmp")) / f"{APP}-{uid}"
    return home / ".config" / APP, home / ".local" / "state" / APP, runtime


def pid_alive(pid: int) -> bool:
    """Whether a process with this ID exists. Never signals or terminates it."""
    if IS_WINDOWS:
        # os.kill(pid, 0) would *terminate* the process on Windows.
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def try_lock(fd: int) -> bool:
    """Take an exclusive lock on the open file without waiting. False if another holder has it."""
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def lock(fd: int) -> None:
    """Take an exclusive lock on the open file, waiting as long as needed."""
    if IS_WINDOWS:
        while not try_lock(fd):
            time.sleep(0.1)
        return
    fcntl.flock(fd, fcntl.LOCK_EX)


def unlock(fd: int) -> None:
    """Release a lock taken with try_lock or lock (Windows releases late on close otherwise)."""
    if IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    fcntl.flock(fd, fcntl.LOCK_UN)
