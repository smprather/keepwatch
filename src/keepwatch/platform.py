"""Everything that differs between POSIX and Windows. Standard library only.

The rest of keepwatch calls these functions instead of OS-specific APIs.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO

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


def absolute_path(value: str | None) -> Path | None:
    if value and os.path.isabs(value):
        return Path(value)
    return None


def default_app_dirs(env: Mapping[str, str], uid: int | None = None) -> tuple[Path, Path, Path]:
    """keepwatch's config, state and runtime directories when no XDG variable overrides them."""
    if IS_WINDOWS:
        home = Path(env.get("USERPROFILE") or Path.home())
        roaming = absolute_path(env.get("APPDATA")) or home / "AppData" / "Roaming"
        local = absolute_path(env.get("LOCALAPPDATA")) or home / "AppData" / "Local"
        return roaming / APP, local / APP, local / APP / "run"
    uid = os.getuid() if uid is None else uid
    home = Path(env.get("HOME") or Path.home())
    runtime = (absolute_path(env.get("TMPDIR")) or Path("/tmp")) / f"{APP}-{uid}"
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


TERMINATED_EXIT = 0xC000013A  # STATUS_CONTROL_C_EXIT: what keepwatch-terminated processes exit with on Windows
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # no console window for children

if IS_WINDOWS:
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _new_job() -> int:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        return job


class HookProcess:
    """A child process and everything it starts, stoppable as one unit."""

    def __init__(self, popen: subprocess.Popen[bytes], job: int | None = None) -> None:
        self.popen = popen
        self.pid = popen.pid
        self.terminated = False
        self._job = job
        self._lock = threading.Lock()

    def terminate(self) -> None:
        """POSIX: SIGTERM to the process group. Windows: end the whole job now (no graceful signal exists)."""
        self._stop(signal.SIGTERM if not IS_WINDOWS else None)

    def kill(self) -> None:
        """POSIX: SIGKILL to the process group. Windows: end the whole job."""
        self._stop(signal.SIGKILL if not IS_WINDOWS else None)

    def _stop(self, sig: signal.Signals | None) -> None:
        with self._lock:
            self.terminated = True
            if IS_WINDOWS:
                if self._job is not None:
                    _kernel32.TerminateJobObject(self._job, TERMINATED_EXIT)
                return
            try:
                os.killpg(self.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def close(self) -> None:
        """Release OS resources once the process has ended (on Windows this ends leftover children)."""
        with self._lock:
            if IS_WINDOWS and self._job is not None:
                _kernel32.CloseHandle(self._job)
                self._job = None


def start_process(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    inherit: Sequence[int] = (),
) -> HookProcess:
    """Start argv in its own process group (POSIX) or Job Object (Windows), passing `inherit` fds."""
    if not IS_WINDOWS:
        popen = subprocess.Popen(
            list(argv), cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
            pass_fds=tuple(inherit), start_new_session=True,
        )
        return HookProcess(popen)
    handles = []
    for fd in inherit:
        handle = msvcrt.get_osfhandle(fd)
        os.set_handle_inheritable(handle, True)
        handles.append(handle)
    startupinfo = subprocess.STARTUPINFO()
    if handles:
        startupinfo.lpAttributeList = {"handle_list": handles}
    popen = subprocess.Popen(
        list(argv), cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr, startupinfo=startupinfo,
        creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        job = _new_job()
    except OSError:
        popen.kill()
        raise
    if not _kernel32.AssignProcessToJobObject(job, int(popen._handle)):
        error = ctypes.get_last_error()
        popen.kill()
        _kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    status = _ntdll.NtResumeProcess(int(popen._handle))
    if status != 0:
        popen.kill()
        _kernel32.CloseHandle(job)
        raise OSError(f"cannot resume the new process (NTSTATUS 0x{status & 0xFFFFFFFF:08x})")
    return HookProcess(popen, job)


def pipe_arguments(read_fd: int, write_fd: int) -> list[str]:
    """Worker command-line arguments naming the request and result pipes."""
    if IS_WINDOWS:
        return [
            "--request-handle", str(msvcrt.get_osfhandle(read_fd)),
            "--result-handle", str(msvcrt.get_osfhandle(write_fd)),
        ]
    return ["--request-fd", str(read_fd), "--result-fd", str(write_fd)]


def open_inherited(value: int, *, handle: bool, mode: str) -> Any:
    """In the worker: open an fd (POSIX) or handle (Windows) passed by the parent, in binary mode."""
    if handle:
        flags = os.O_RDONLY if "r" in mode else os.O_WRONLY
        value = msvcrt.open_osfhandle(value, flags)
    return os.fdopen(value, mode, buffering=0) if "w" in mode else os.fdopen(value, mode)


_POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
_SCRIPT_RUNNERS = {
    ".ps1": [*_POWERSHELL, "-File"],
    ".cmd": ["cmd.exe", "/d", "/c"],
    ".bat": ["cmd.exe", "/d", "/c"],
}


def default_shell() -> list[str]:
    """The program and leading arguments that run a string hook or a ctx.run string."""
    if IS_WINDOWS:
        return [*_POWERSHELL, "-Command"]
    return ["/bin/sh", "-c"]


# Windows PowerShell writes redirected output in the console code page; keepwatch reads UTF-8.
_UTF8_PRELUDE = (
    "try { $utf8 = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8 } catch { }; "
)


def shell_argv(text: str, shell: Sequence[str] | None = None) -> list[str]:
    """How to run a string command: `shell` if given, else the platform shell (UTF-8 output on Windows)."""
    if shell:
        return [*shell, text]
    if IS_WINDOWS:
        return [*_POWERSHELL, "-Command", _UTF8_PRELUDE + text]
    return ["/bin/sh", "-c", text]


def has_path_separator(program: str) -> bool:
    return "/" in program or (IS_WINDOWS and "\\" in program)


def command_argv(argv: Sequence[str], base: Path) -> list[str]:
    """Resolve a list hook's relative program against `base`; on Windows, choose the script's interpreter."""
    program, *rest = argv
    if has_path_separator(program) and not os.path.isabs(program):
        program = str(base / program)
    if IS_WINDOWS:
        suffix = Path(program).suffix.lower()
        if suffix == ".py":
            return [sys.executable, program, *rest]
        if suffix in _SCRIPT_RUNNERS:
            return [*_SCRIPT_RUNNERS[suffix], program, *rest]
    return [program, *rest]


def points_to(link: Path, target: Path) -> bool:
    """Whether `link` is a directory that resolves to `target`."""
    try:
        return link.is_dir() and os.path.normcase(os.path.realpath(link)) == os.path.normcase(str(target))
    except OSError:
        return False


def replace_junction(link: Path, target: Path) -> None:
    """Windows: make `link` a directory junction to `target`, replacing a wrong link. Needs no special rights."""
    import _winapi

    if link.is_junction() or link.is_symlink():
        os.rmdir(link)  # removes only the link
    elif link.exists():
        raise FileExistsError(f"{link} exists and is not a link")
    _winapi.CreateJunction(str(target), str(link))


if IS_WINDOWS:
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


def open_shared(path: Path) -> BinaryIO:
    """Open a file for reading without stopping others from renaming or deleting it (matters on Windows)."""
    if not IS_WINDOWS:
        return open(path, "rb")
    handle = _kernel32.CreateFileW(
        str(path), _GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in (2, 3):  # file or path not found
            raise FileNotFoundError(2, "No such file or directory", str(path))
        raise ctypes.WinError(error)
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb")


def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """os.replace; on Windows retried for up to a second while another process briefly holds the target."""
    attempts = 20 if IS_WINDOWS else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
