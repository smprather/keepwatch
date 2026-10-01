# keepwatch Plan 5b (Windows Behaviour) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make keepwatch's behaviour correct on Windows: the platform shell for string hooks and `ctx.run`, interpreters for script files, a junction-based worker shim, file sharing that lets logs rotate, a portable `keepwatch stop`, running without a console, and `install`/`uninstall` through Task Scheduler.

**Architecture:** New functions in `platform.py` (`default_shell`, `command_argv`, `has_path_separator`, `open_shared`, `replace`, `points_to`, `replace_junction`), a new `winsched.py` that builds PowerShell scripts for Task Scheduler / Startup-folder setup, a `stop.request` file the service checks every second, and `keepwatch/__main__.py`. POSIX behaviour is unchanged except where noted (string hooks and `ctx.run` strings now go through the same platform shell, `/bin/sh -c`, as before).

**Tech Stack:** as Plan 5a.

**Spec:** `docs/superpowers/specs/2026-10-01-keepwatch-windows-design.md` (sections 3, 4, 6). Builds on Plan 5a (implemented).

## Global Constraints

- All Global Constraints of Plan 5a apply (branch `keepwatch-windows`, explicit `git add`, Linux suite green before every commit, do not push, Windows-only code copied exactly and verified by the supervisor on CI).
- New tests are portable unless marked: build commands from `sys.executable` (`[sys.executable, "-c", ...]` or `.py` helper files), and write paths into TOML with `Path(...).as_posix()` inside single-quoted (literal) strings, because `\` is an escape in double-quoted TOML.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A string hook must work unchanged on both OSes when it uses syntax both shells share (`exit 3`). Test: Task 1 `test_string_hooks_use_the_platform_shell`.
- `keepwatch stop` must stop a running service on every OS. Test: Task 4 `test_run_polls_and_stops_cleanly`.
- Following the log must not stop rotation on Windows. Test: Task 3 `test_open_shared_allows_rename`.

---

### Task 1: Platform shell and script interpreters

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/config.py`, `src/keepwatch/runner.py`, `src/keepwatch/ctx.py`, `src/keepwatch/validation.py`, `src/keepwatch/reference.py`
- Test: `tests/test_portable_hooks.py`, `tests/test_ctx.py`

**Interfaces:**
- Produces:
  - `platform.default_shell() -> list[str]` — `["/bin/sh", "-c"]`, or `["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]` on Windows
  - `platform.has_path_separator(program: str) -> bool` — `/`, or also `\` on Windows
  - `platform.command_argv(argv: Sequence[str], base: Path) -> list[str]` — a relative program containing a path separator becomes `base / program`; on Windows `.py` runs with `sys.executable`, `.ps1` with PowerShell `-File`, `.cmd`/`.bat` with `cmd.exe /d /c`
  - watch config key `shell` (kind `argv`: a non-empty list of strings; defaultable), `WatchConfig.shell: tuple[str, ...] | None = None`
  - `Command.to_argv(shell: Sequence[str] | None = None)` — string hooks run as `[*shell_or_default, string]`
  - `Ctx.run` runs a string through `platform.default_shell()` and always decodes output as UTF-8 (with replacement)

- [ ] **Step 1: Write the failing tests** — `tests/test_portable_hooks.py`

```python
import sys
from pathlib import Path

import pytest

from keepwatch.runner import Runner

UNKNOWN_3 = "[check_exit_codes]\nunknown = [3]\n"


def test_string_hooks_use_the_platform_shell(make_watch, call_for):
    watch_dir = make_watch("s", config='[hooks]\ncheck = "exit 3"\n' + UNKNOWN_3)
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_shell_key_overrides_the_platform_shell(make_watch, call_for):
    exe = Path(sys.executable).as_posix()
    watch_dir = make_watch(
        "s", config=f"shell = ['{exe}', '-c']\n[hooks]\ncheck = 'import sys; sys.exit(3)'\n" + UNKNOWN_3
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_relative_python_program_runs_from_the_watch_dir(make_watch, call_for):
    watch_dir = make_watch(
        "s",
        config='[hooks]\ncheck = ["./helper.py", "3"]\n' + UNKNOWN_3,
        files={"helper.py": f"#!{sys.executable}\nimport sys\nsys.exit(int(sys.argv[1]))\n"},
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"


def test_shell_must_be_a_non_empty_list(tmp_path):
    from keepwatch.config import ConfigError, load_watch_config

    watch_dir = tmp_path / "w"
    watch_dir.mkdir()
    (watch_dir / "config.toml").write_text("shell = []\n")
    with pytest.raises(ConfigError, match="'shell' must be a non-empty list of strings"):
        load_watch_config(watch_dir)


@pytest.mark.windows_only
def test_powershell_and_cmd_scripts(make_watch, call_for):
    watch_dir = make_watch(
        "s",
        config='[hooks]\ncheck = ["./check.ps1"]\non_true = ["./act.cmd"]\n' + UNKNOWN_3,
        files={"check.ps1": "exit 3\n", "act.cmd": "@echo off\r\nexit /b 0\r\n"},
    )
    assert Runner().run(call_for(watch_dir)).status == "unknown"
    assert Runner().run(call_for(watch_dir, hook="on_true")).status == "ok"
```

In `tests/test_ctx.py`, add `import sys` to the imports if missing, replace the whole `test_run_decodes_non_utf8_output` test (from Plan 4) with the portable version below, and append the second test:

```python
def test_run_decodes_non_utf8_output(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted)
    code = "import sys; sys.stdout.buffer.write(bytes([255]) + b'ok')"
    assert ctx.run([sys.executable, "-c", code]).stdout == "\ufffdok"
    assert emitted[0]["stdout"] == "\ufffdok"


def test_run_string_uses_the_platform_shell(tmp_path):
    assert make_ctx(tmp_path).run("exit 3", check=False).returncode == 3
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_portable_hooks.py tests/test_ctx.py -q` → FAIL (`shell` is an unknown key; the `.py` helper is not resolved against the watch dir and fails to start)

- [ ] **Step 3: Implement.**

In `src/keepwatch/platform.py` add `import sys` to the imports and append:

```python
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
```

In `src/keepwatch/config.py`:
- add `from keepwatch import platform` to the imports;
- add a key to `WATCH_KEYS` after `python_dependencies`:

```python
    Key(
        "shell",
        "argv",
        None,
        "Program and leading arguments that run string hooks, e.g. [\"pwsh\", \"-NoProfile\", \"-Command\"]. "
        "Default: /bin/sh -c on POSIX, Windows PowerShell on Windows.",
        True,
    ),
```

- add a field `shell: tuple[str, ...] | None = None` to `WatchConfig` after `python_dependencies`;
- in `_convert`, add before the final `raise AssertionError`'s preceding `except` block — i.e. as another `if` inside the `try`, after the `str_list` case:

```python
        if key.kind == "argv":
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                raise _Bad("a non-empty list of strings")
            return tuple(value)
```

- replace `Command`'s docstring and `to_argv` with:

```python
    """A command hook: argv runs directly; shell runs through the platform shell (or the watch's `shell`)."""

    argv: tuple[str, ...] | None = None
    shell: str | None = None

    def to_argv(self, shell: Sequence[str] | None = None) -> list[str]:
        if self.shell is not None:
            return [*(shell or platform.default_shell()), self.shell]
        return list(self.argv or ())
```

(add `Sequence` to the `collections.abc` import);
- in `_command`, change the message's first part to `f"'{key}' must be a command: a non-empty string (run by the shell: /bin/sh -c, or PowerShell on Windows) "`.

In `src/keepwatch/reference.py`, add `"argv": "list of strings (program and arguments)",` to `_KIND_NAMES`.

In `src/keepwatch/runner.py`, in `_run_command`, replace `command.to_argv(),` in the `platform.start_process(...)` call with `argv,` and insert before that call (inside the same `try` that starts the process, or right before it):

```python
            argv = command.to_argv(call.watch.shell)
            if command.argv is not None:
                argv = platform.command_argv(argv, call.watch.watch_dir)
```

In `src/keepwatch/ctx.py`, add `from keepwatch import platform` to the imports, change the `run` docstring's second sentence to "argv: a list runs directly; a string runs through the platform shell (/bin/sh -c, or Windows PowerShell).", and replace the `subprocess.run(...)` call's first arguments so the call reads:

```python
            completed = subprocess.run(
                [*platform.default_shell(), args] if shell else args,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                env={**os.environ, **(env or {})},
                cwd=cwd if cwd is not None else self.watch_dir,
            )
```

(the `shell=shell` and `text=True` arguments are removed).

In `src/keepwatch/validation.py`, add `from keepwatch import platform` and replace the start of `missing_executable`'s path branch:

```python
    program = command.argv[0]
    if platform.has_path_separator(program):
        path = Path(program) if os.path.isabs(program) else watch.watch_dir / program
        if not path.is_file():
            return f"{program} does not exist"
        if not platform.IS_WINDOWS and not os.access(path, os.X_OK):
            return f"{program} is not executable (run: chmod +x {path})"
        return None
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/config.py src/keepwatch/runner.py src/keepwatch/ctx.py src/keepwatch/validation.py src/keepwatch/reference.py tests/test_portable_hooks.py tests/test_ctx.py
git commit -m "Platform shell for string hooks and ctx.run; script interpreters and relative programs"
```

---

### Task 2: Worker shim without symlink rights on Windows

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/runner.py`
- Test: `tests/test_runner_python.py`

**Interfaces:**
- Produces: `platform.points_to(link: Path, target: Path) -> bool`; `platform.replace_junction(link: Path, target: Path) -> None` (Windows only). `make_shim` uses a directory junction on Windows (no special rights needed), serialised with `hold_lock(directory / ".shim.lock")`; POSIX keeps the atomic symlink replacement.

- [ ] **Step 1: Write the failing test.** In `tests/test_runner_python.py`, mark `test_make_shim_is_safe_across_threads_and_repairs_dangling_links` with `@pytest.mark.posix_only` (creating symlinks needs special rights on Windows) and append:

```python
@pytest.mark.windows_only
def test_make_shim_repairs_a_wrong_junction_across_threads(tmp_path):
    import _winapi

    import keepwatch

    lib = tmp_path / "lib"
    lib.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _winapi.CreateJunction(str(other), str(lib / "keepwatch"))
    errors = []

    def build():
        try:
            make_shim(lib)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (lib / "keepwatch").resolve() == Path(keepwatch.__file__).resolve().parent
```

- [ ] **Step 2: Run** — `uv run pytest tests/test_runner_python.py -q` → PASS on Linux (the new test is Windows-only; it fails on Windows CI until Step 3)

- [ ] **Step 3: Implement.** Append to `src/keepwatch/platform.py`:

```python
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
```

In `src/keepwatch/runner.py`, add `from keepwatch.locks import hold_lock` to the imports and replace `make_shim` with:

```python
def make_shim(directory: Path) -> Path:
    """A PYTHONPATH directory exposing only the running keepwatch package (for uv environments)."""
    import keepwatch

    target = Path(keepwatch.__file__).resolve().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = directory / "keepwatch"
    if platform.IS_WINDOWS:
        # Symlinks need special rights on Windows; a junction does not, but it cannot be replaced atomically.
        with hold_lock(directory / ".shim.lock"):
            if not platform.points_to(link, target):
                platform.replace_junction(link, target)
        return directory
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return directory
    temp = directory / f".keepwatch-{os.getpid()}-{threading.get_ident()}"
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, link)  # atomic: concurrent callers never see a missing or half-made link
    return directory
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/runner.py tests/test_runner_python.py
git commit -m "Worker shim: a directory junction on Windows"
```

---

### Task 3: Files that others may rename while we read them

**Files:**
- Modify: `src/keepwatch/platform.py`, `src/keepwatch/logquery.py`, `src/keepwatch/logstore.py`, `src/keepwatch/paths.py`, `src/keepwatch/ctx.py`
- Test: `tests/test_platform.py`, `tests/test_logstore.py`

**Interfaces:**
- Produces: `platform.open_shared(path: Path) -> BinaryIO` (on Windows opened with read, write and delete sharing, so others can still rename or delete the file; raises `FileNotFoundError` for a missing file); `platform.replace(src, dst) -> None` (`os.replace`, retried for up to 1 s on `PermissionError` on Windows). `logs` reads and follows the log through `open_shared`; log rotation that hits `PermissionError` is skipped until a later write; `write_json_atomic` and ledgers use `platform.replace`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_platform.py` (add `import threading` to its imports):

```python
def test_open_shared_allows_rename(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    with platform.open_shared(path) as handle:
        path.rename(tmp_path / "b.txt")
        assert handle.read() == b"x"
    with pytest.raises(FileNotFoundError):
        platform.open_shared(tmp_path / "missing.txt")


def test_replace(tmp_path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.write_text("new")
    target.write_text("old")
    platform.replace(source, target)
    assert target.read_text() == "new" and not source.exists()


@pytest.mark.windows_only
def test_replace_retries_while_the_target_is_briefly_open(tmp_path):
    source, target = tmp_path / "s", tmp_path / "t"
    source.write_text("new")
    target.write_text("old")
    handle = open(target, "rb")
    threading.Timer(0.3, handle.close).start()
    platform.replace(source, target)
    assert target.read_text() == "new"
```

Append to `tests/test_logstore.py` (add `import pytest` to its imports if missing):

```python
@pytest.mark.windows_only
def test_rotation_waits_while_another_process_holds_the_log(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=200, backups=2)
    writer.write({"event": "first", "pad": "x" * 150})
    with open(path, "rb"):  # an ordinary reader blocks renames on Windows
        for n in range(5):
            writer.write({"event": "more", "n": n, "pad": "y" * 150})
    assert len(path.read_text().splitlines()) == 6
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_platform.py -q` → FAIL (`AttributeError: ... 'open_shared'`)

- [ ] **Step 3: Implement.** In `src/keepwatch/platform.py`, add `from typing import BinaryIO` to the imports, add to the `if IS_WINDOWS:` setup block of Task 5a (or a new one after it):

```python
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
```

and append:

```python
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
```

In `src/keepwatch/logquery.py`, add `from keepwatch import platform` and replace `open(file, "rb")` in `read_records` and `open(path, "rb")` in `follow_log.open_current` with `platform.open_shared(file)` and `platform.open_shared(path)`.

In `src/keepwatch/logstore.py`, wrap the body of `_rotate` so a sharing violation skips rotation:

```python
    def _rotate(self) -> None:
        try:
            if self.backups <= 0:
                self.path.unlink(missing_ok=True)
                return
            self._backup(self.backups).unlink(missing_ok=True)
            for index in range(self.backups - 1, 0, -1):
                source = self._backup(index)
                if source.exists():
                    source.rename(self._backup(index + 1))
            self.path.rename(self._backup(1))
        except PermissionError:
            # Windows: another process has the log open; rotate on a later write instead.
            return
```

In `src/keepwatch/paths.py` (`write_json_atomic`) and `src/keepwatch/ctx.py` (`Ledger._save`), replace `os.replace(temp, ...)` with `platform.replace(temp, ...)` (add `from keepwatch import platform` to `ctx.py`; `paths.py` already imports it).

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/platform.py src/keepwatch/logquery.py src/keepwatch/logstore.py src/keepwatch/paths.py src/keepwatch/ctx.py tests/test_platform.py tests/test_logstore.py
git commit -m "Share files so logs can rotate while read; retry replaces briefly blocked on Windows"
```

---

### Task 4: `keepwatch stop` and running without a console

**Files:**
- Modify: `src/keepwatch/paths.py`, `src/keepwatch/service.py`, `src/keepwatch/cli.py`, `src/keepwatch/logstore.py`
- Test: `tests/test_service.py`, `tests/test_cli_run.py`

**Interfaces:**
- Produces: `Paths.stop_request -> Path` (`runtime / "stop.request"`); `Service.run` checks the stop request every second (and still ticks every `reload_interval`), logs `service.stop_requested`, removes the file and stops; `Service.start` removes a stale stop request; `keepwatch stop [--timeout SECONDS]` (exit 0 once the service lock is free, 1 if no service runs or it did not stop in time); `run` writes only to the log file when `sys.stdout` is `None`; `QueueSink` never assumes a console.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_service.py` (add `import threading` and `import time` to its imports if missing):

```python
def test_stop_request_file_stops_the_service(xdg, make_watch):
    make_watch("a", config='[hooks]\ncheck = ["true"]\n')
    records = []
    service = make_service(xdg, records)
    thread = threading.Thread(target=service.run, args=(threading.Event(),), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not xdg.status_file.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    xdg.stop_request.parent.mkdir(parents=True, exist_ok=True)
    xdg.stop_request.write_text("now")
    thread.join(15)
    assert not thread.is_alive()
    assert not xdg.stop_request.exists()
    assert [r["event"] for r in records][-2:] == ["service.stop_requested", "service.stop"]
```

(`["true"]` is a list hook that exists on POSIX only, but this test never polls — `start_threads=False` — so it is portable.)

In `tests/test_cli_run.py`: rename the existing `test_run_polls_and_stops_cleanly` to `test_run_stops_on_sigterm` and mark it `@pytest.mark.posix_only` (add `import pytest`); then add this portable test:

```python
def test_run_polls_and_stops_cleanly(xdg, make_watch):
    watch_dir = make_watch("w", config='interval = "1s"\n', files={"watch.py": COUNTER})
    count = watch_dir / "count"
    proc = subprocess.Popen([*KEEPWATCH, "run"], env=dict(os.environ), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert wait_for(lambda: count.exists() and int(count.read_text()) >= 2)
        second = subprocess.run([*KEEPWATCH, "run"], env=dict(os.environ), capture_output=True, text=True, timeout=30)
        assert second.returncode == 1
        assert "already running" in second.stdout + second.stderr
        stopped = subprocess.run([*KEEPWATCH, "stop"], env=dict(os.environ), capture_output=True, text=True,
                                 timeout=60)
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "stopped" in stopped.stdout
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out + err
    events = [json.loads(line)["event"] for line in xdg.log_file.read_text().splitlines()]
    assert "service.stop_requested" in events
    assert events[-1] == "service.stop"


def test_stop_without_a_service(xdg):
    result = CliRunner().invoke(cli, ["stop"])
    assert result.exit_code == 1
    assert "no keepwatch service is running" in result.output
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_service.py tests/test_cli_run.py -q` → FAIL (`AttributeError: 'Paths' object has no attribute 'stop_request'`; `No such command 'stop'`)

- [ ] **Step 3: Implement.**

In `src/keepwatch/paths.py`, add to `Paths` after `service_lock`:

```python
    @property
    def stop_request(self) -> Path:
        return self.runtime / "stop.request"
```

In `src/keepwatch/service.py`, replace `run` with:

```python
    def run(self, stop: threading.Event) -> None:
        """start(); then tick every reload_interval until `stop` is set or a stop request arrives; then stop()."""
        self.start()
        try:
            next_tick = time.monotonic() + self.global_config.reload_interval
            while not stop.wait(1.0):
                if self._stop_requested():
                    break
                if time.monotonic() >= next_tick:
                    self.tick()
                    next_tick = time.monotonic() + self.global_config.reload_interval
        finally:
            self.stop()

    def _stop_requested(self) -> bool:
        """True (once) when `keepwatch stop` has written the stop request file."""
        try:
            self.paths.stop_request.unlink()
        except OSError:
            return False
        self._sink(make_record("service.stop_requested"))
        return True
```

and add `self.paths.stop_request.unlink(missing_ok=True)` as the first line of `start()`.

In `src/keepwatch/logstore.py`, in `QueueSink._drain`, replace `traceback.print_exc()` with:

```python
                if sys.stderr is not None:  # no console under pythonw
                    traceback.print_exc()
```

(add `import sys`).

In `src/keepwatch/cli.py`:
- add `import sys` and change the statusview import to `from keepwatch.statusview import collect_status, format_status, service_running`;
- change the Run panel to `{"name": "Run", "commands": ["run", "stop"]}`;
- replace `_fail` with:

```python
def _fail(message: str) -> NoReturn:
    if sys.stderr is not None:  # no console under pythonw
        make_console(stderr=True).print(message)
    raise SystemExit(1)
```

- in `run_service`, replace the two lines that build `printer` and `sink` with:

```python
        if sys.stdout is None:  # pythonw: no console, the log file has everything
            sink = QueueSink(writer.write)
        else:
            printer = level_filter(ConsolePrinter(make_console(), verbose=verbose), minimum)
            sink = QueueSink(fan_out(writer.write, printer))
```

- add this helper and command before `def main()`:

```python
def _request_stop(paths: Paths, timeout: float) -> bool:
    """Ask a running service to stop; True once it has (its lock is free)."""
    paths.stop_request.parent.mkdir(parents=True, exist_ok=True)
    paths.stop_request.write_text(str(time.time()), encoding="utf-8")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not service_running(paths):
            return True
        time.sleep(0.2)
    return False


@cli.command()
@click.option("--timeout", type=float, default=30.0, show_default=True, help="Seconds to wait for the service to stop.")
@click.pass_obj
def stop(app: App, timeout: float) -> None:
    """Ask the running service to stop, and wait until it has. Works on every OS (the way to stop it on Windows).

    Running hooks are terminated and the service exits once their polls end.

    Exit status: 0 when the service has stopped, 1 if none is running or it did not stop in time.
    """
    if not service_running(app.paths):
        _fail("no keepwatch service is running")
    if _request_stop(app.paths, timeout):
        click.echo("keepwatch service stopped")
    else:
        _fail(f"the service did not stop within {timeout:g}s; see: keepwatch logs --since 5m")
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/paths.py src/keepwatch/service.py src/keepwatch/cli.py src/keepwatch/logstore.py tests/test_service.py tests/test_cli_run.py
git commit -m "Add keepwatch stop; run without a console"
```

---

### Task 5: `python -m keepwatch`

**Files:**
- Create: `src/keepwatch/__main__.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing test** — `tests/test_main.py`

```python
import subprocess
import sys

from keepwatch import __version__


def test_python_dash_m_keepwatch():
    result = subprocess.run([sys.executable, "-m", "keepwatch", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert __version__ in result.stdout
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_main.py -q` → FAIL (`No module named keepwatch.__main__`)

- [ ] **Step 3: Implement** — `src/keepwatch/__main__.py`

```python
"""`python -m keepwatch` (used by the Windows logon task, which runs pythonw.exe -m keepwatch run)."""

from keepwatch.cli import main

main()
```

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/__main__.py tests/test_main.py
git commit -m "Support python -m keepwatch"
```

---

### Task 6: `install` and `uninstall` on Windows

**Files:**
- Create: `src/keepwatch/winsched.py`
- Modify: `src/keepwatch/cli.py`
- Test: `tests/test_cli_install.py`, `tests/test_winsched.py`

**Interfaces:**
- Produces:
  - `winsched.TASK_NAME = "keepwatch"`; `pythonw() -> str` (the `pythonw.exe` next to the running interpreter, else the interpreter); `run_arguments(config_path: Path | None) -> str` (`-m keepwatch [--config "<path>"] run`); `register_script(executable, arguments) -> str`; `shortcut_script(executable, arguments) -> str`; `unregister_script() -> str`; `run_powershell(script) -> CompletedProcess[str]` (`$KEEPWATCH_POWERSHELL` or `powershell.exe`)
  - `cli._on_windows() -> bool` (returns `platform.IS_WINDOWS`; tests patch it); on Windows `install` registers the logon task, falls back to a Startup-folder shortcut, and prints which; `--dry-run` prints both scripts; `uninstall` stops a running service, then removes the task and the shortcut

- [ ] **Step 1: Write the failing tests.** In `tests/test_cli_install.py`, mark every existing test `@pytest.mark.posix_only` (they drive a fake `systemctl` shell script). Add to the imports at the top of the file: `import subprocess as _subprocess`, `import pytest`, `from keepwatch import cli as cli_module` and `from keepwatch import winsched` (imports stay at the top; `uv run ruff check --fix` sorts them). Then append:

```python
def on_windows(monkeypatch, exit_codes=()):
    scripts = []
    codes = list(exit_codes)

    def fake_powershell(script):
        scripts.append(script)
        code = codes.pop(0) if codes else 0
        return _subprocess.CompletedProcess(["powershell.exe"], code, "", "refused by policy" if code else "")

    monkeypatch.setattr(cli_module, "_on_windows", lambda: True)
    monkeypatch.setattr(winsched, "run_powershell", fake_powershell)
    return scripts


def test_windows_install_registers_a_logon_task(xdg, monkeypatch):
    scripts = on_windows(monkeypatch)
    result = run("install")
    assert result.exit_code == 0, result.output
    (script,) = scripts
    assert "Register-ScheduledTask" in script and "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-m keepwatch run" in script
    assert "registered Task Scheduler task 'keepwatch'" in result.output


def test_windows_install_falls_back_to_a_startup_shortcut(xdg, monkeypatch):
    scripts = on_windows(monkeypatch, exit_codes=(1, 0))
    result = run("install")
    assert result.exit_code == 0, result.output
    assert "CreateShortcut" in scripts[1]
    assert "Startup-folder shortcut" in result.output


def test_windows_install_fails_when_both_methods_fail(xdg, monkeypatch):
    on_windows(monkeypatch, exit_codes=(1, 1))
    result = run("install")
    assert result.exit_code == 1
    assert "refused by policy" in result.output


def test_windows_install_dry_run_and_custom_config(xdg, tmp_path, monkeypatch):
    scripts = on_windows(monkeypatch)
    custom = tmp_path / "custom.toml"
    custom.write_text("")
    result = run("--config", str(custom), "install", "--dry-run")
    assert result.exit_code == 0
    assert scripts == []
    assert "Register-ScheduledTask" in result.output and "CreateShortcut" in result.output
    assert f'--config "{custom.resolve()}" run' in result.output


def test_windows_uninstall(xdg, monkeypatch):
    scripts = on_windows(monkeypatch)
    result = run("uninstall")
    assert result.exit_code == 0, result.output
    (script,) = scripts
    assert "Unregister-ScheduledTask" in script and "keepwatch.lnk" in script
```

`tests/test_winsched.py`:

```python
from pathlib import Path

from keepwatch import winsched


def test_quoting_doubles_single_quotes():
    script = winsched.register_script("C:/it's/pythonw.exe", "-m keepwatch run")
    assert "'C:/it''s/pythonw.exe'" in script


def test_run_arguments():
    assert winsched.run_arguments(None) == "-m keepwatch run"
    assert winsched.run_arguments(Path("C:/cfg/k.toml")) == f'-m keepwatch --config "{Path("C:/cfg/k.toml")}" run'
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_cli_install.py tests/test_winsched.py -q` → FAIL (`ModuleNotFoundError: No module named 'keepwatch.winsched'`)

- [ ] **Step 3: Implement** — `src/keepwatch/winsched.py`

```python
"""Windows: start keepwatch at logon with Task Scheduler, or a Startup-folder shortcut as a fallback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "keepwatch"
_SHORTCUT = "(Join-Path ([Environment]::GetFolderPath('Startup')) 'keepwatch.lnk')"


def pythonw() -> str:
    """pythonw.exe next to the running interpreter (no console window), else the interpreter itself."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else Path(sys.executable))


def _quote(text: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + text.replace("'", "''") + "'"


def run_arguments(config_path: Path | None) -> str:
    config = f' --config "{config_path}"' if config_path is not None else ""
    return f"-m keepwatch{config} run"


def register_script(executable: str, arguments: str) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute {_quote(executable)} -Argument {_quote(arguments)}",
            '$user = "$env:USERDOMAIN\\$env:USERNAME"',
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user",
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew",
            "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
            f"Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Action $action -Trigger $trigger "
            "-Settings $settings -Principal $principal -Force | Out-Null",
            f"Start-ScheduledTask -TaskName {_quote(TASK_NAME)}",
        ]
    )


def shortcut_script(executable: str, arguments: str) -> str:
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$path = {_SHORTCUT}",
            "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($path)",
            f"$shortcut.TargetPath = {_quote(executable)}",
            f"$shortcut.Arguments = {_quote(arguments)}",
            "$shortcut.WindowStyle = 7",
            "$shortcut.Save()",
            f"Start-Process -FilePath {_quote(executable)} -ArgumentList {_quote(arguments)} -WindowStyle Hidden",
        ]
    )


def unregister_script() -> str:
    return "\n".join(
        [
            f"Unregister-ScheduledTask -TaskName {_quote(TASK_NAME)} -Confirm:$false -ErrorAction SilentlyContinue",
            f"Remove-Item -LiteralPath {_SHORTCUT} -ErrorAction SilentlyContinue",
        ]
    )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    """Run a script with Windows PowerShell ($KEEPWATCH_POWERSHELL replaces powershell.exe)."""
    program = os.environ.get("KEEPWATCH_POWERSHELL", "powershell.exe")
    return subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        errors="replace",
    )
```

In `src/keepwatch/cli.py`:
- change `from keepwatch import __version__, systemd` to `from keepwatch import __version__, platform, systemd, winsched`;
- add:

```python
def _on_windows() -> bool:
    return platform.IS_WINDOWS


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no details"


def _install_windows(app: App, dry_run: bool) -> None:
    custom_config = app.config_path.resolve() if app.config_path != app.paths.config_file else None
    executable = winsched.pythonw()
    arguments = winsched.run_arguments(custom_config)
    register = winsched.register_script(executable, arguments)
    shortcut = winsched.shortcut_script(executable, arguments)
    if dry_run:
        click.echo("would run in PowerShell:\n")
        click.echo(register)
        click.echo("\nand, if Task Scheduler refuses, create a Startup-folder shortcut:\n")
        click.echo(shortcut)
        return
    result = winsched.run_powershell(register)
    if result.returncode == 0:
        click.echo(f"registered Task Scheduler task '{winsched.TASK_NAME}' (at logon, restarted on failure) and started it")
    else:
        click.echo(f"Task Scheduler refused ({_last_line(result.stderr)}); using a Startup-folder shortcut instead")
        fallback = winsched.run_powershell(shortcut)
        if fallback.returncode != 0:
            _fail(f"could not create the Startup-folder shortcut either: {_last_line(fallback.stderr)}")
        click.echo("created a Startup-folder shortcut (keepwatch.lnk) and started keepwatch")
    click.echo("check it with: keepwatch status")
```

- at the top of `install`'s body (after the docstring) add:

```python
    if _on_windows():
        _install_windows(app, dry_run)
        return
```

- at the top of `uninstall`'s body add:

```python
    if _on_windows():
        if service_running(app.paths) and not _request_stop(app.paths, 30.0):
            click.echo("warning: the running service did not stop within 30s")
        result = winsched.run_powershell(winsched.unregister_script())
        if result.returncode != 0:
            _fail(f"could not remove the logon task: {_last_line(result.stderr)}")
        click.echo("removed the logon task and the Startup-folder shortcut (whichever existed)")
        click.echo("keepwatch will no longer start at logon")
        return
```

- replace the first line of the `install` docstring with `"""Start keepwatch at login: a systemd user service on Linux, a Task Scheduler logon task on Windows.` and add a paragraph: `On Windows it registers a Task Scheduler task "keepwatch" that runs pythonw.exe -m keepwatch run at logon (no console window, restarted on failure); if policy forbids that, it creates a Startup-folder shortcut instead.` Replace the first line of the `uninstall` docstring with `"""Stop and remove what keepwatch install set up (systemd user service, or Windows logon task/shortcut).`

- [ ] **Step 4: Run the suite and ruff** — `uv run pytest -q` → PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/winsched.py src/keepwatch/cli.py tests/test_cli_install.py tests/test_winsched.py
git commit -m "install/uninstall on Windows: Task Scheduler logon task with a Startup-folder fallback"
```
