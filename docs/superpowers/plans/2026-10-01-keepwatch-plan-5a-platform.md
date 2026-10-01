# keepwatch Plan 5a (Platform Layer and CI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put every operating-system difference behind `keepwatch/platform.py` (directories, locks, process liveness, process trees, worker pipe passing), so keepwatch imports and runs its core on Windows, and add CI on Linux and Windows.

**Architecture:** `platform.py` exposes OS-neutral functions; on Windows it uses `msvcrt`, `ctypes` Job Objects and `STARTUPINFO.lpAttributeList["handle_list"]`; on POSIX it keeps today's behaviour exactly. `paths.py`, `locks.py`, `runner.py` and `worker.py` call it instead of POSIX APIs.

**Tech Stack:** Python ≥ 3.12 stdlib (`ctypes`, `msvcrt` on Windows), uv, pytest, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-windows-design.md` (sections 3, 5, 8). Base: release 2026.10.1.

## Global Constraints

- Work on branch `keepwatch-windows`. Commit with explicit `git add`; never `git commit -a`. Do not push (the supervisor pushes to run CI).
- Behaviour on Linux must not change; the existing suite must stay green on Linux after every task.
- `platform.py`, `worker.py`, `ctx.py`, `protocol.py`, `hooks.py`, `durations.py` stay standard-library only.
- Windows-only code cannot run locally. Copy it exactly as written; the supervisor verifies it on Windows CI. Tests that only make sense on one OS use the `posix_only` / `windows_only` markers from Task 1.
- `uv run ruff check src tests` must pass before every commit (`--fix` allowed for I001/W29x only).
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- `pid_alive` must never terminate a process on Windows (`os.kill(pid, 0)` does). Test: Task 2 `test_pid_alive`.
- Killing a hook must kill its grandchildren on both OSes. Test: Task 4 `test_kill_takes_the_whole_tree`.
- Linux behaviour unchanged: the whole existing suite stays green after every task.

---

### Task 1: CI workflow and OS markers

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `tests/conftest.py`
- Test: `tests/test_markers.py`

- [ ] **Step 1: Write the failing test** — `tests/test_markers.py`

```python
import os

import pytest


@pytest.mark.posix_only
def test_posix_marker_runs_only_on_posix():
    assert os.name != "nt"


@pytest.mark.windows_only
def test_windows_marker_runs_only_on_windows():
    assert os.name == "nt"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_markers.py -q` → 1 FAIL (`test_windows_marker_runs_only_on_windows` runs and fails on Linux)

- [ ] **Step 3: Implement.** Add to `tests/conftest.py` (keep existing fixtures; add `import os` if missing):

```python
IS_WINDOWS = os.name == "nt"


def pytest_configure(config):
    config.addinivalue_line("markers", "posix_only: needs a POSIX system (signals, sh, Expect, systemd)")
    config.addinivalue_line("markers", "windows_only: Windows-specific behaviour")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "posix_only" in item.keywords and IS_WINDOWS:
            item.add_marker(pytest.mark.skip(reason="POSIX only"))
        if "windows_only" in item.keywords and not IS_WINDOWS:
            item.add_marker(pytest.mark.skip(reason="Windows only"))
```

Create `.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main, "keepwatch-*"]
    tags: ["v*"]
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.12", "3.14"]
    runs-on: ${{ matrix.os }}
    # Windows is informational until the port is complete (plan 5c removes this line).
    continue-on-error: ${{ matrix.os == 'windows-latest' }}
    env:
      UV_PYTHON: ${{ matrix.python }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv sync
      - run: uv run pytest -q -ra
      - if: runner.os == 'Linux' && matrix.python == '3.12'
        run: uv run ruff check src tests
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS (the Windows marker test is skipped on Linux)

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml tests/conftest.py tests/test_markers.py
git commit -m "Add CI on Linux and Windows, and OS markers for tests"
```

---

### Task 2: `platform.py` — directories and process liveness

**Files:**
- Create: `src/keepwatch/platform.py`
- Modify: `src/keepwatch/paths.py`
- Test: `tests/test_platform.py`, `tests/test_paths.py`

**Interfaces:**
- Produces: `platform.IS_WINDOWS: bool`; `platform.default_app_dirs(env: Mapping[str, str], uid: int | None = None) -> tuple[Path, Path, Path]` (config, state, runtime — already ending in `keepwatch`); `platform.pid_alive(pid: int) -> bool`. `paths.resolve_paths` keeps its signature and XDG behaviour; `paths.pid_alive` is re-exported from `platform`; `ensure_private_dir` skips the owner/mode checks on Windows.

- [ ] **Step 1: Write the failing tests** — `tests/test_platform.py`

```python
import os
import subprocess
import sys
from pathlib import Path

import pytest

from keepwatch import platform


def test_pid_alive():
    assert platform.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert platform.pid_alive(child.pid) is False


@pytest.mark.posix_only
def test_posix_default_dirs():
    config, state, runtime = platform.default_app_dirs({"HOME": "/home/u", "TMPDIR": "/var/tmp"}, uid=7)
    assert (config, state, runtime) == (
        Path("/home/u/.config/keepwatch"),
        Path("/home/u/.local/state/keepwatch"),
        Path("/var/tmp/keepwatch-7"),
    )


@pytest.mark.windows_only
def test_windows_default_dirs():
    env = {"APPDATA": r"C:\Users\u\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    config, state, runtime = platform.default_app_dirs(env)
    assert config == Path(r"C:\Users\u\AppData\Roaming\keepwatch")
    assert state == Path(r"C:\Users\u\AppData\Local\keepwatch")
    assert runtime == Path(r"C:\Users\u\AppData\Local\keepwatch\run")
```

In `tests/test_paths.py`, add `import pytest` if missing and mark these tests `@pytest.mark.posix_only` (they test POSIX defaults and permissions): `test_resolve_paths_defaults_and_ignores_relative_values`, `test_runtime_fallback_without_tmpdir`, `test_ensure_private_dir_creates_mode_700`, `test_ensure_private_dir_rejects_shared_directory`, `test_ensure_private_dir_rejects_symlink`. Add:

```python
@pytest.mark.windows_only
def test_xdg_variables_override_windows_defaults(tmp_path):
    paths = resolve_paths({"XDG_CONFIG_HOME": str(tmp_path / "c"), "XDG_STATE_HOME": str(tmp_path / "s"),
                           "XDG_RUNTIME_DIR": str(tmp_path / "r"), "APPDATA": r"C:\x", "LOCALAPPDATA": r"C:\y"})
    assert paths == Paths(tmp_path / "c" / "keepwatch", tmp_path / "s" / "keepwatch", tmp_path / "r" / "keepwatch")
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_platform.py tests/test_paths.py -q` → FAIL (`ImportError: cannot import name 'platform' from 'keepwatch'`)

- [ ] **Step 3: Implement.** Create `src/keepwatch/platform.py`:

```python
"""Everything that differs between POSIX and Windows. Standard library only.

The rest of keepwatch calls these functions instead of OS-specific APIs.
"""

from __future__ import annotations

import os
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
```

In `src/keepwatch/paths.py`:
- add `from keepwatch import platform` to the imports, and `from keepwatch.platform import pid_alive  # noqa: F401  (re-exported)` (keep the name importable from `keepwatch.paths`);
- delete the local `_absolute` and `pid_alive` functions only if nothing else in `paths.py` uses them — keep `_absolute` (used below);
- replace `resolve_paths` with:

```python
def resolve_paths(env: Mapping[str, str] | None = None, uid: int | None = None) -> Paths:
    """keepwatch's directories: XDG variables when set, else the platform defaults."""
    env = os.environ if env is None else env
    config_default, state_default, runtime_default = platform.default_app_dirs(env, uid)
    config_base = _absolute(env.get("XDG_CONFIG_HOME"))
    state_base = _absolute(env.get("XDG_STATE_HOME"))
    runtime_base = _absolute(env.get("XDG_RUNTIME_DIR"))
    return Paths(
        config_base / APP if config_base else config_default,
        state_base / APP if state_base else state_default,
        runtime_base / APP if runtime_base else runtime_default,
    )
```

- in `ensure_private_dir`, insert right after the `if not stat.S_ISDIR(info.st_mode):` check:

```python
    if platform.IS_WINDOWS:
        return path  # the user profile's ACL already keeps these directories private
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/paths.py tests/test_platform.py tests/test_paths.py
git commit -m "Add the platform layer: directories and process liveness"
```

---

### Task 3: Portable file locks

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/locks.py`
- Test: `tests/test_locks.py` (unchanged tests must pass on both OSes)

**Interfaces:**
- Produces: `platform.try_lock(fd: int) -> bool`, `platform.lock(fd: int) -> None` (blocks), `platform.unlock(fd: int) -> None`. `hold_lock` keeps its signature and behaviour.

- [ ] **Step 1: Confirm the existing lock tests are the specification** — `uv run pytest tests/test_locks.py -q` → PASS (they must still pass after the change, on Linux now and on Windows in CI)

- [ ] **Step 2: Implement.** Append to `src/keepwatch/platform.py` (add `import time` to its imports):

```python
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
```

Replace `src/keepwatch/locks.py` with:

```python
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
```

- [ ] **Step 3: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 4: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/locks.py
git commit -m "Portable file locks: flock on POSIX, msvcrt on Windows"
```

---

### Task 4: Process trees and worker pipes

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/runner.py`, `src/keepwatch/worker.py`
- Test: `tests/test_platform.py`

**Interfaces:**
- Produces:
  - `platform.HookProcess` — wraps a `subprocess.Popen` (`.popen`, `.pid`) and owns its whole process tree: `terminate()` (POSIX: SIGTERM to the group; Windows: terminate the Job Object), `kill()` (POSIX: SIGKILL to the group; Windows: terminate the job), `close()` (Windows: release the job, which kills anything left in it; POSIX: nothing). `terminated: bool` is set by `terminate()`/`kill()`.
  - `platform.start_process(argv, *, cwd, env, stdin, stdout, stderr, inherit: Sequence[int] = ()) -> HookProcess` — POSIX: new session, `pass_fds=inherit`; Windows: started suspended with no window, assigned to a new Job Object with kill-on-close, then resumed; `inherit` fds are passed as inheritable handles via `handle_list`.
  - `platform.pipe_arguments(read_fd: int, write_fd: int) -> list[str]` — `["--request-fd", r, "--result-fd", w]` on POSIX, `["--request-handle", h, "--result-handle", h]` on Windows.
  - `platform.open_inherited(value: int, *, handle: bool, mode: str)` — reopen a passed fd/handle as a binary file object in the worker.
  - `platform.TERMINATED_EXIT = 0xC000013A` — the exit code of processes keepwatch terminates on Windows.
  - `runner` uses `HookProcess` everywhere (the `_active` set holds `HookProcess` objects); on Windows a process that exits with `TERMINATED_EXIT` after keepwatch terminated it is reported like a signal on POSIX: status `error`/`failed`, reason `terminated by keepwatch`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_platform.py` (add `import time` to its imports):

```python
GRANDCHILD = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


def test_kill_takes_the_whole_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    process = platform.start_process(
        [sys.executable, "-c", GRANDCHILD, str(pid_file)],
        cwd=tmp_path, env=None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline, "the grandchild never started"
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    process.kill()
    process.popen.wait(10)
    process.close()
    assert process.terminated is True
    deadline = time.monotonic() + 10
    while platform.pid_alive(grandchild):
        assert time.monotonic() < deadline, "the grandchild survived"
        time.sleep(0.05)


def test_pipe_arguments_name_the_os_mechanism():
    read_fd, write_fd = os.pipe()
    try:
        args = platform.pipe_arguments(read_fd, write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    expected = ("--request-handle", "--result-handle") if platform.IS_WINDOWS else ("--request-fd", "--result-fd")
    assert (args[0], args[2]) == expected
```

(On Linux, a zombie grandchild counts as gone only once reaped; `pid_alive` uses `os.kill(pid, 0)`, which succeeds for zombies. The grandchild is re-parented to init, which reaps it, so the loop ends. If this test is flaky on Linux, stop and report.)

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_platform.py -q` → FAIL (`AttributeError: module 'keepwatch.platform' has no attribute 'start_process'`)

- [ ] **Step 3: Implement.** Append to `src/keepwatch/platform.py` (add `import signal`, `import subprocess`, `import threading` and `from collections.abc import Sequence` to its imports; `typing.IO` and `Any` as needed):

```python
TERMINATED_EXIT = 0xC000013A  # STATUS_CONTROL_C_EXIT: what keepwatch-terminated processes exit with on Windows

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
    _ntdll.NtResumeProcess(int(popen._handle))
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
```

(`msvcrt` is already imported on Windows by Task 3's block; `Any` comes from `typing`.)

In `src/keepwatch/worker.py`, replace the two `parser.add_argument` lines and the two `os.fdopen` uses in `main` with:

```python
    parser.add_argument("--request-fd", type=int)
    parser.add_argument("--result-fd", type=int)
    parser.add_argument("--request-handle", type=int)
    parser.add_argument("--result-handle", type=int)
    args = parser.parse_args(argv)
    by_handle = args.request_handle is not None
    request_source = args.request_handle if by_handle else args.request_fd
    result_target = args.result_handle if by_handle else args.result_fd
    if request_source is None or result_target is None:
        parser.error("give --request-fd/--result-fd or --request-handle/--result-handle")
    with platform.open_inherited(request_source, handle=by_handle, mode="rb") as handle:
        request = json.loads(handle.read())
    out = platform.open_inherited(result_target, handle=by_handle, mode="wb")
```

(keeping the existing `args = parser.parse_args(argv)` line removed, since it is now part of the block above), and add `from keepwatch import platform` to the worker's imports. Update the worker's module docstring: "Reads one request (JSON) from the request pipe (`--request-fd`, or `--request-handle` on Windows) … writes JSON-line messages to the result pipe".

In `src/keepwatch/runner.py`:
- update the module docstring's second sentence to: "Every call gets its own process group (POSIX) or Job Object (Windows). At the deadline the whole tree is stopped: SIGTERM, then SIGKILL after kill_grace seconds on POSIX; at once on Windows.";
- add `from keepwatch import platform` and `from keepwatch.platform import HookProcess` to the imports; delete the `_signal_group` function;
- in `Runner.__init__`, change `self._active: set[int] = set()` to `self._active: set[HookProcess] = set()`;
- replace `terminate_all`, `kill_all`, `_track` and `_untrack` with:

```python
    def terminate_all(self) -> None:
        """Stop every running hook gracefully (POSIX SIGTERM; immediately on Windows)."""
        with self._active_lock:
            processes = list(self._active)
        for process in processes:
            process.terminate()

    def kill_all(self) -> None:
        """Shutdown, step 2: forcibly stop every hook still running."""
        with self._active_lock:
            processes = list(self._active)
        for process in processes:
            process.kill()

    def _track(self, process: HookProcess) -> None:
        with self._active_lock:
            self._active.add(process)
            closing = self._closing
        if closing:
            # Started while close() ran: stop it right away.
            process.terminate()

    def _untrack(self, process: HookProcess) -> None:
        with self._active_lock:
            self._active.discard(process)
        process.close()
```

(`close` stays as it is: it sets `_closing` and calls `terminate_all`.)
- replace `_supervise` with:

```python
    def _supervise(self, process: HookProcess, readers: Sequence[threading.Thread], timeout: float) -> tuple[int, bool]:
        timed_out = False
        try:
            process.popen.wait(timeout=max(timeout, 0.0))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.popen.wait(timeout=self.kill_grace)
            except subprocess.TimeoutExpired:
                pass
            process.kill()
            process.popen.wait()
        drain_until = time.monotonic() + DRAIN_GRACE
        for reader in readers:
            reader.join(max(drain_until - time.monotonic(), 0.0))
        if any(reader.is_alive() for reader in readers):
            # A leftover child still holds a pipe open: stop the whole tree.
            process.kill()
            for reader in readers:
                reader.join(DRAIN_GRACE)
        return process.popen.returncode, timed_out
```

- add this helper method to `Runner` (after `_captured`):

```python
    @staticmethod
    def _stopped_reason(process: HookProcess, returncode: int) -> str | None:
        """Why a process ended abnormally because of a signal or keepwatch, or None."""
        if returncode < 0:
            return f"killed by signal {_signal_name(-returncode)}"
        if platform.IS_WINDOWS and process.terminated and returncode == platform.TERMINATED_EXIT:
            return "terminated by keepwatch"
        return None
```

- in `_WorkerRun`, add a field `process: HookProcess` (last). In `_start_worker`, replace the `tail = [...]` line with `tail = ["-m", "keepwatch.worker", *platform.pipe_arguments(req_r, res_w)]`, replace the `subprocess.Popen(...)` call with

```python
            process = platform.start_process(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=call.watch.watch_dir,
                env=env,
                inherit=(req_r, res_w),
            )
```

then use `process.popen.stdout` / `process.popen.stderr` for the readers, `self._track(process)`, `self._supervise(process, ...)`, `self._untrack(process)`, and return `_WorkerRun(returncode, timed_out, out, err, messages, process)`;
- in `_worker_result`, directly after `if run.timed_out: return ...`, add:

```python
        stopped = self._stopped_reason(run.process, run.returncode)
        if stopped is not None and results.result is None:
            return HookResult(status=failure, reason=stopped, **common)
```

- in `_run_command`, replace the `subprocess.Popen(...)` call with `process = platform.start_process(command.to_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=call.watch.watch_dir, env=env)`, use `process.popen.stdout/stderr`, `self._track(process)`, `self._supervise(process, (out, err), call.timeout)`, `self._untrack(process)`, and replace

```python
            if returncode < 0:
                return HookResult(
                    status=self._failure_status(call),
                    reason=f"killed by signal {_signal_name(-returncode)}",
                    **common,
                )
```

with

```python
            stopped = self._stopped_reason(process, returncode)
            if stopped is not None:
                return HookResult(status=self._failure_status(call), reason=stopped, **common)
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS (all existing runner tests unchanged on Linux)

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/runner.py src/keepwatch/worker.py tests/test_platform.py
git commit -m "Process trees and worker pipes through the platform layer (Job Objects and handle lists on Windows)"
```
