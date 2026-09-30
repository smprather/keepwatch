# keepwatch Plan 1 (Core Engine and Development Loop) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build keepwatch's core — config loading, the state machine, the plugin API, isolated hook execution and logging — and expose it through `keepwatch poll` and `keepwatch validate`, so a watch can be written and exercised end to end.

**Architecture:** Pure modules (durations, state) sit under I/O modules (paths, config, runner, logstore). Every hook call runs in a fresh child process in its own process group: Python hooks through `python -m keepwatch.worker` (stdlib-only child code talking JSON lines over dedicated pipes), command hooks directly. A `PollEngine` runs one poll (check → state machine → actions) and emits JSON log records to sinks; the rich-click CLI wires sinks to the log file and the terminal.

**Tech Stack:** Python ≥ 3.12, uv (project and venv), hatchling (build backend), rich-click 1.9 (CLI; brings click and rich), pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-keepwatch-design.md`

## Global Constraints

- Python `>=3.12`; runtime dependency `rich-click>=1.9` only; dev dependency `pytest>=8`.
- `keepwatch.durations`, `keepwatch.hooks`, `keepwatch.protocol`, `keepwatch.ctx`, `keepwatch.worker` and `keepwatch/__init__.py` import only the standard library and each other (they run inside plugin environments).
- Indent with 4 spaces; never tab characters.
- Durations: a number (seconds) or strings of integer+unit pairs, units `s m h d` (`"30s"`, `"1h30m"`).
- Paths: `$XDG_CONFIG_HOME/keepwatch`, `$XDG_STATE_HOME/keepwatch`, `$XDG_RUNTIME_DIR/keepwatch` (fallback `$TMPDIR/keepwatch-<uid>`, mode 0700 enforced).
- Defaults: `interval` 60s (minimum 1s), `check_timeout` 60s, `action_timeout` 60s, `max_failures` 5, `reload_interval` 5s, log `max_bytes` 10_000_000, `backups` 10, `capture_bytes` 65_536, payload limit 1 MiB.
- Timeouts: SIGTERM to the hook's process group, SIGKILL 5 seconds later.
- Error text format: `<file>:<line>: <problem>; see: keepwatch docs <topic>` (omit `:<line>` when unknown).
- CLI exit codes: 0 success, 1 problems found or a poll failed, 2 usage error.
- Output not on a terminal (or with `NO_COLOR` set): no color and no box-drawing characters.
- Every commit message ends with the attribution trailer your harness specifies.

## Review Focus

- A hook that leaves a background process holding its stdout (e.g. `sleep 30 &`) must not hang the poll: the runner returns within a few seconds and kills the leftover. Test: Task 9 `test_background_child_holding_stdout_does_not_hang`.
- A check that returns `pathlib.Path` objects in its payload (the natural result of `ctx.glob`) must work: paths become strings. Test: Task 6 `test_normalize_payload_converts_paths_and_tuples`.
- A hook printing non-UTF-8 bytes must be logged (with replacement characters), not crash the runner. Test: Task 9 `test_binary_output_is_decoded_with_replacement`.
- A `watch.py` with a syntax error or failing import must produce an `error` outcome naming the line or exception, not a traceback from keepwatch. Tests: Task 3 `test_resolve_hooks_reports_syntax_error_and_conflicts`, Task 8 `test_import_error_is_reported`, Task 11 `test_syntax_error_in_watch_py_is_an_error_outcome`.
- A half-edited `config.toml` (typo, missing value) must report every problem with a line number and a suggestion. Tests: Task 3 `test_typo_reports_line_and_suggestion`, `test_all_problems_reported_at_once`, `test_invalid_toml_reports_line`.

---

## File Structure

```
pyproject.toml                    project metadata, entry point keepwatch = keepwatch.cli:main
src/keepwatch/__init__.py         __version__ and the public plugin API re-exports
src/keepwatch/durations.py        parse_duration / format_duration
src/keepwatch/paths.py            Paths (XDG), ensure_private_dir, stale process dir cleanup
src/keepwatch/locks.py            hold_lock (flock) and LockBusy
src/keepwatch/hooks.py            hook names; AST discovery of Python hooks; resolve_hooks
src/keepwatch/config.py           watch + global config schema, parsing, discovery
src/keepwatch/state.py            Outcome, WatchState, plan_poll, finish_poll, backoff
src/keepwatch/protocol.py         JSON-lines messages, clip, normalize_payload
src/keepwatch/ctx.py              Ctx, Ledger, Unknown, CommandFailed, LedgerReadOnly
src/keepwatch/worker.py           child side of a Python hook call
src/keepwatch/runner.py           HookCall, HookResult, Runner (worker + command hooks)
src/keepwatch/logstore.py         make_record, fan_out, LogWriter (rotation)
src/keepwatch/output.py           plain/rich detection, format_record, ConsolePrinter
src/keepwatch/pollengine.py       Fake, parse_fakes, PollReport, PollEngine
src/keepwatch/validation.py       WatchCheck, validate_watches
src/keepwatch/cli.py              rich-click group, poll, validate
tests/conftest.py                 xdg, make_watch, call_for fixtures
tests/test_*.py                   one test file per module (CLI split per command)
```

---

### Task 1: Project scaffold and durations

**Files:**
- Create: `pyproject.toml`
- Create: `src/keepwatch/__init__.py`
- Create: `src/keepwatch/durations.py`
- Test: `tests/test_durations.py`

**Interfaces:**
- Produces: `keepwatch.__version__: str = "0.1.0"`; `parse_duration(value: object) -> float`; `format_duration(seconds: float) -> str`; `DurationError(ValueError)`.

- [ ] **Step 1: Create the project files**

`pyproject.toml`:

```toml
[project]
name = "keepwatch"
version = "0.1.0"
description = "Poll conditions and run actions, from login onward."
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
dependencies = ["rich-click>=1.9"]

[project.scripts]
keepwatch = "keepwatch.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/keepwatch"]

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`src/keepwatch/__init__.py`:

```python
"""keepwatch: poll conditions and run actions, from login onward."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Write the failing test** — `tests/test_durations.py`

```python
import re

import pytest

from keepwatch.durations import DurationError, format_duration, parse_duration


@pytest.mark.parametrize(
    "value, seconds",
    [
        ("30s", 30),
        ("15m", 900),
        ("1h", 3600),
        ("90d", 90 * 86400),
        ("1h30m", 5400),
        (" 2M ", 120),
        (45, 45),
        (1.5, 1.5),
        (0, 0),
    ],
)
def test_parse_duration_accepts(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    "value, fragment",
    [
        ("30", 'needs a unit: "30s"'),
        ("", "expected a duration"),
        ("1x", "expected a duration"),
        ("m5", "expected a duration"),
        ("1.5h", "expected a duration"),
        (-1, "must not be negative"),
        (True, "expected a duration"),
        (None, "expected a duration"),
    ],
)
def test_parse_duration_rejects(value, fragment):
    with pytest.raises(DurationError, match=re.escape(fragment)):
        parse_duration(value)


@pytest.mark.parametrize(
    "seconds, text",
    [(90, "1m30s"), (3600, "1h"), (0, "0s"), (0.5, "0.5s"), (86401, "1d1s")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv sync && uv run pytest tests/test_durations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.durations'`

- [ ] **Step 4: Implement** — `src/keepwatch/durations.py`

```python
"""Parse and format durations such as "30s", "15m", "1h30m" or a number of seconds."""

from __future__ import annotations

import re

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_WHOLE = re.compile(r"(?:\d+[dhms])+")
_PART = re.compile(r"(\d+)([dhms])")


class DurationError(ValueError):
    """Raised when a value is not a valid duration."""


def parse_duration(value: object) -> float:
    """Return a duration in seconds.

    Accepts a non-negative int or float (seconds), or a string of one or more
    integer+unit pairs: "30s", "15m", "1h30m", "90d". Units: s, m, h, d.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise DurationError(f'expected a duration like "30s", got {value!r}')
    if isinstance(value, (int, float)):
        if value < 0:
            raise DurationError(f"duration must not be negative, got {value!r}")
        return float(value)
    text = value.strip().lower()
    if text.isdigit():
        raise DurationError(
            f'duration {value!r} needs a unit: "{text}s" for seconds (units: s, m, h, d)'
        )
    if not _WHOLE.fullmatch(text):
        raise DurationError(
            f'expected a duration like "30s", "15m" or "1h30m" (units: s, m, h, d), got {value!r}'
        )
    return float(sum(int(count) * _UNIT_SECONDS[unit] for count, unit in _PART.findall(text)))


def format_duration(seconds: float) -> str:
    """Format seconds compactly: 90 -> "1m30s", 3600 -> "1h", 0.5 -> "0.5s"."""
    if seconds != int(seconds):
        return f"{seconds:g}s"
    remaining = int(seconds)
    if remaining == 0:
        return "0s"
    parts = []
    for unit in ("d", "h", "m", "s"):
        count, remaining = divmod(remaining, _UNIT_SECONDS[unit])
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_durations.py -q`
Expected: PASS (all parametrized cases)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/keepwatch/__init__.py src/keepwatch/durations.py tests/test_durations.py
git commit -m "Scaffold keepwatch package and duration parsing"
```

---

### Task 2: Paths, private directories and locks

**Files:**
- Create: `src/keepwatch/paths.py`
- Create: `src/keepwatch/locks.py`
- Create: `tests/conftest.py`
- Test: `tests/test_paths.py`, `tests/test_locks.py`

**Interfaces:**
- Produces:
  - `Paths(config_home: Path, state_home: Path, runtime: Path)` (frozen dataclass) with properties `config_file`, `default_watches_dir`, `logs_dir`, `log_file`, `status_file`, `service_lock` and methods `watch_state_dir(name)`, `watch_data_dir(name)`, `offline_file(name)`, `watch_lock(name)`, `process_dir(pid)`, `run_dir(pid, name)` — all returning `Path`.
  - `resolve_paths(env: Mapping[str, str] | None = None, uid: int | None = None) -> Paths`
  - `ensure_private_dir(path: Path) -> Path`, raising `PathError`
  - `pid_alive(pid: int) -> bool`; `remove_stale_process_dirs(paths: Paths, alive=pid_alive) -> list[Path]`
  - `hold_lock(path: Path, *, blocking: bool = True, on_wait: Callable[[], None] | None = None)` context manager; `LockBusy`
  - Test fixtures `xdg` (a `Paths` for a temporary XDG tree, env vars set) and `make_watch(name, config="", files=None, base=None) -> Path`.

- [ ] **Step 1: Write the fixtures** — `tests/conftest.py`

```python
import textwrap

import pytest

from keepwatch.paths import Paths, resolve_paths


@pytest.fixture
def xdg(tmp_path, monkeypatch) -> Paths:
    """Point every XDG variable at a private temporary tree."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("KEEPWATCH_CONFIG", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    return resolve_paths()


@pytest.fixture
def make_watch(xdg):
    """Create a watch directory; files starting with '#!' become executable."""

    def make(name, config="", files=None, base=None):
        watch_dir = (base or xdg.default_watches_dir) / name
        watch_dir.mkdir(parents=True)
        (watch_dir / "config.toml").write_text(textwrap.dedent(config))
        for relative, content in (files or {}).items():
            path = watch_dir / relative
            text = textwrap.dedent(content).lstrip("\n")
            path.write_text(text)
            if text.startswith("#!"):
                path.chmod(0o755)
        return watch_dir

    return make
```

- [ ] **Step 2: Write the failing tests** — `tests/test_paths.py`

```python
import stat
from pathlib import Path

import pytest

from keepwatch.paths import (
    PathError,
    Paths,
    ensure_private_dir,
    remove_stale_process_dirs,
    resolve_paths,
)


def test_resolve_paths_uses_xdg_variables():
    paths = resolve_paths(
        {
            "HOME": "/home/u",
            "XDG_CONFIG_HOME": "/cfg",
            "XDG_STATE_HOME": "/st",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        uid=1000,
    )
    assert paths == Paths(Path("/cfg/keepwatch"), Path("/st/keepwatch"), Path("/run/user/1000/keepwatch"))


def test_resolve_paths_defaults_and_ignores_relative_values():
    paths = resolve_paths({"HOME": "/home/u", "XDG_CONFIG_HOME": "relative", "TMPDIR": "/var/tmp"}, uid=1234)
    assert paths.config_home == Path("/home/u/.config/keepwatch")
    assert paths.state_home == Path("/home/u/.local/state/keepwatch")
    assert paths.runtime == Path("/var/tmp/keepwatch-1234")


def test_runtime_fallback_without_tmpdir():
    assert resolve_paths({"HOME": "/h"}, uid=7).runtime == Path("/tmp/keepwatch-7")


def test_derived_paths():
    paths = Paths(Path("/c"), Path("/s"), Path("/r"))
    assert paths.config_file == Path("/c/config.toml")
    assert paths.default_watches_dir == Path("/c/watches")
    assert paths.log_file == Path("/s/logs/keepwatch.jsonl")
    assert paths.status_file == Path("/s/status.json")
    assert paths.service_lock == Path("/r/service.lock")
    assert paths.watch_data_dir("w") == Path("/s/watches/w/data")
    assert paths.offline_file("w") == Path("/s/watches/w/offline.json")
    assert paths.watch_lock("w") == Path("/r/locks/w.lock")
    assert paths.run_dir(42, "w") == Path("/r/42/w")


def test_ensure_private_dir_creates_mode_700(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_private_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_ensure_private_dir_rejects_shared_directory(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    target.chmod(0o755)
    with pytest.raises(PathError, match="chmod 700"):
        ensure_private_dir(target)


def test_ensure_private_dir_rejects_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(PathError, match="not a directory"):
        ensure_private_dir(link)


def test_remove_stale_process_dirs(tmp_path):
    paths = Paths(tmp_path / "c", tmp_path / "s", tmp_path / "r")
    for name in ("111", "222", "notes"):
        (paths.runtime / name).mkdir(parents=True)
    removed = remove_stale_process_dirs(paths, alive=lambda pid: pid == 111)
    assert removed == [paths.runtime / "222"]
    assert sorted(p.name for p in paths.runtime.iterdir()) == ["111", "notes"]
```

`tests/test_locks.py`:

```python
import threading

import pytest

from keepwatch.locks import LockBusy, hold_lock


def test_second_nonblocking_lock_is_busy(tmp_path):
    lock = tmp_path / "locks" / "w.lock"
    with hold_lock(lock):
        with pytest.raises(LockBusy):
            with hold_lock(lock, blocking=False):
                pass
    with hold_lock(lock, blocking=False):
        pass


def test_on_wait_is_called_when_blocked(tmp_path):
    lock = tmp_path / "w.lock"
    ready = threading.Event()
    release = threading.Event()

    def holder():
        with hold_lock(lock):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    ready.wait(5)
    threading.Timer(0.2, release.set).start()
    waited = []
    with hold_lock(lock, on_wait=lambda: waited.append(True)):
        pass
    thread.join()
    assert waited == [True]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_paths.py tests/test_locks.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.paths'`

- [ ] **Step 4: Implement** — `src/keepwatch/paths.py`

```python
"""Where keepwatch keeps its files (XDG base directories)."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "keepwatch"


class PathError(Exception):
    """A directory keepwatch needs is unsafe or cannot be created."""


@dataclass(frozen=True)
class Paths:
    config_home: Path
    state_home: Path
    runtime: Path

    @property
    def config_file(self) -> Path:
        return self.config_home / "config.toml"

    @property
    def default_watches_dir(self) -> Path:
        return self.config_home / "watches"

    @property
    def logs_dir(self) -> Path:
        return self.state_home / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "keepwatch.jsonl"

    @property
    def status_file(self) -> Path:
        return self.state_home / "status.json"

    @property
    def service_lock(self) -> Path:
        return self.runtime / "service.lock"

    def watch_state_dir(self, watch: str) -> Path:
        return self.state_home / "watches" / watch

    def watch_data_dir(self, watch: str) -> Path:
        return self.watch_state_dir(watch) / "data"

    def offline_file(self, watch: str) -> Path:
        return self.watch_state_dir(watch) / "offline.json"

    def watch_lock(self, watch: str) -> Path:
        return self.runtime / "locks" / f"{watch}.lock"

    def process_dir(self, pid: int) -> Path:
        return self.runtime / str(pid)

    def run_dir(self, pid: int, watch: str) -> Path:
        return self.process_dir(pid) / watch


def _absolute(value: str | None) -> Path | None:
    # The XDG spec says relative paths in these variables are invalid and must be ignored.
    if value and os.path.isabs(value):
        return Path(value)
    return None


def resolve_paths(env: Mapping[str, str] | None = None, uid: int | None = None) -> Paths:
    """Compute keepwatch's directories from the XDG environment variables."""
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    home = Path(env.get("HOME") or Path.home())
    config_base = _absolute(env.get("XDG_CONFIG_HOME")) or home / ".config"
    state_base = _absolute(env.get("XDG_STATE_HOME")) or home / ".local" / "state"
    runtime_base = _absolute(env.get("XDG_RUNTIME_DIR"))
    if runtime_base is not None:
        runtime = runtime_base / APP
    else:
        runtime = (_absolute(env.get("TMPDIR")) or Path("/tmp")) / f"{APP}-{uid}"
    return Paths(config_base / APP, state_base / APP, runtime)


def ensure_private_dir(path: Path) -> Path:
    """Create path (mode 0700) if needed and verify that only the current user can use it."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise PathError(f"cannot create {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PathError(f"{path} exists and is not a directory")
    if info.st_uid != os.getuid():
        raise PathError(f"{path} is owned by uid {info.st_uid}, not you; refusing to use it")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise PathError(f"{path} is accessible by other users (mode {mode:o}); run: chmod 700 {path}")
    return path


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_stale_process_dirs(paths: Paths, alive: Callable[[int], bool] = pid_alive) -> list[Path]:
    """Delete run-only directories left behind by keepwatch processes that no longer exist."""
    if not paths.runtime.is_dir():
        return []
    removed = []
    for entry in paths.runtime.iterdir():
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink() and not alive(int(entry.name)):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return sorted(removed)
```

`src/keepwatch/locks.py`:

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_paths.py tests/test_locks.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/paths.py src/keepwatch/locks.py tests/conftest.py tests/test_paths.py tests/test_locks.py
git commit -m "Add XDG paths, private directory checks and flock helpers"
```

---

### Task 3: Hook discovery and watch config

**Files:**
- Create: `src/keepwatch/hooks.py`
- Create: `src/keepwatch/config.py`
- Test: `tests/test_config_watch.py`

**Interfaces:**
- Consumes: `parse_duration`, `DurationError` (Task 1).
- Produces:
  - `hooks.CHECK = "check"`, `hooks.ACTION_HOOKS`, `hooks.HOOK_NAMES`, `hooks.WATCH_PY`, `hooks.NO_CHECK: str`
  - `discover_python_hooks(watch_dir: Path) -> frozenset[str]` (raises `SyntaxError`/`ValueError`)
  - `resolve_hooks(watch_dir: Path, command_hooks: Collection[str]) -> tuple[frozenset[str], str | None]` — defined hooks and a problem message or `None`
  - `config.ConfigProblem(path, line, message, topic="config")` with `__str__` in the constraint format; `ConfigError(problems: list[ConfigProblem])` with `.problems`
  - `Command(argv: tuple[str, ...] | None = None, shell: str | None = None)` with `to_argv() -> list[str]`, `display() -> str`
  - `ExitCodes(true=(0,), false=(1,), unknown=())`
  - `Key(name, kind, default, doc, defaultable=False)`; `WATCH_KEYS: tuple[Key, ...]`; `WATCH_TABLES: dict[str, str]`
  - `WatchConfig` (frozen) fields: `name, watch_dir, description, enabled, interval, initial_condition, check_timeout, action_timeout, max_failures, retry_after, python_dependencies, hooks: Mapping[str, Command], exit_codes: ExitCodes, environment: Mapping[str, str], settings: Mapping[str, Any]`; property `config_file`
  - `load_watch_config(watch_dir: Path, defaults: Mapping[str, Any] | None = None) -> WatchConfig`

- [ ] **Step 1: Write the failing tests** — `tests/test_config_watch.py`

```python
import textwrap

import pytest

from keepwatch.config import Command, ConfigError, ExitCodes, load_watch_config
from keepwatch.hooks import discover_python_hooks, resolve_hooks


def write(watch_dir, text):
    watch_dir.mkdir(parents=True, exist_ok=True)
    (watch_dir / "config.toml").write_text(textwrap.dedent(text))
    return watch_dir


def problems_of(watch_dir, text):
    with pytest.raises(ConfigError) as info:
        load_watch_config(write(watch_dir, text))
    return info.value.problems


def test_empty_config_gives_defaults(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", ""))
    assert cfg.name == "w"
    assert (cfg.interval, cfg.check_timeout, cfg.action_timeout) == (60.0, 60.0, 60.0)
    assert cfg.max_failures == 5
    assert cfg.retry_after is None
    assert cfg.enabled is True and cfg.initial_condition is False
    assert cfg.exit_codes == ExitCodes()
    assert dict(cfg.hooks) == {} and dict(cfg.settings) == {} and dict(cfg.environment) == {}


def test_full_config(tmp_path):
    cfg = load_watch_config(write(tmp_path / "psg-export", '''
        description = "Send exports"
        interval = "30s"
        action_timeout = "15m"
        retry_after = "1h"
        initial_condition = true
        max_failures = 3
        python_dependencies = ["requests>=2.32"]

        [hooks]
        check = "./check.sh"
        on_true = ["expect", "./send.exp"]

        [check_exit_codes]
        unknown = [6, 7, 28]

        [environment]
        SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"

        [settings]
        pattern = "~/incoming/*.tar.gz"
        nested = { a = 1 }
    '''))
    assert cfg.interval == 30.0 and cfg.action_timeout == 900.0 and cfg.retry_after == 3600.0
    assert cfg.initial_condition is True and cfg.max_failures == 3
    assert cfg.python_dependencies == ("requests>=2.32",)
    assert cfg.hooks["check"] == Command(shell="./check.sh")
    assert cfg.hooks["check"].to_argv() == ["/bin/sh", "-c", "./check.sh"]
    assert cfg.hooks["on_true"] == Command(argv=("expect", "./send.exp"))
    assert cfg.hooks["on_true"].to_argv() == ["expect", "./send.exp"]
    assert cfg.hooks["on_true"].display() == "expect ./send.exp"
    assert cfg.exit_codes == ExitCodes(true=(0,), false=(1,), unknown=(6, 7, 28))
    assert cfg.environment["SSH_AUTH_SOCK"] == "/run/user/1000/ssh-agent.socket"
    assert cfg.settings == {"pattern": "~/incoming/*.tar.gz", "nested": {"a": 1}}
    assert cfg.config_file == tmp_path / "psg-export" / "config.toml"


def test_defaults_apply_and_file_wins(tmp_path):
    cfg = load_watch_config(write(tmp_path / "w", 'interval = "10s"\n'), {"interval": 120.0, "max_failures": 9})
    assert cfg.interval == 10.0
    assert cfg.max_failures == 9


def test_typo_reports_line_and_suggestion(tmp_path):
    (problem,) = problems_of(tmp_path / "w", 'description = "x"\nintervall = "30s"\n')
    assert problem.line == 2
    assert problem.message == "unknown key 'intervall' (did you mean 'interval'?)"
    assert str(problem).endswith(
        "config.toml:2: unknown key 'intervall' (did you mean 'interval'?); see: keepwatch docs config"
    )


def test_all_problems_reported_at_once(tmp_path):
    problems = problems_of(tmp_path / "w", 'interval = "soon"\nmax_failures = -1\nenabled = "yes"\n')
    assert [p.line for p in problems] == [1, 2, 3]
    assert 'expected a duration like "30s"' in problems[0].message
    assert problems[1].message == "'max_failures' must be a whole number >= 0, got -1"
    assert problems[2].message == "'enabled' must be true or false, got 'yes'"


def test_interval_minimum(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "interval = 0.5\n")
    assert problem.message == "'interval' must be at least 1s, got 0.5"


def test_invalid_toml_reports_line(tmp_path):
    (problem,) = problems_of(tmp_path / "w", 'interval = "30s"\ncheck_timeout =\n')
    assert problem.line == 2
    assert problem.message.startswith("invalid TOML:")


def test_hooks_table_problems(tmp_path):
    problems = problems_of(tmp_path / "w", '[hooks]\non_ture = "./x"\ncheck = []\n')
    assert (problems[0].line, problems[0].message) == (
        2,
        "unknown key 'on_ture' in [hooks] (did you mean 'on_true'?)",
    )
    assert problems[1].line == 3
    assert problems[1].message.startswith("'check' must be a command")
    assert problems[1].topic == "executables"


def test_exit_code_overlap(tmp_path):
    (problem,) = problems_of(tmp_path / "w", "[check_exit_codes]\nunknown = [1]\n")
    assert problem.line == 1
    assert problem.message == "exit code 1 is listed under both 'false' and 'unknown'"


def test_missing_config_file(tmp_path):
    (tmp_path / "w").mkdir()
    with pytest.raises(ConfigError, match="missing config.toml"):
        load_watch_config(tmp_path / "w")


def test_discover_python_hooks(tmp_path):
    (tmp_path / "watch.py").write_text(
        "def check(ctx):\n    return True\n\n"
        "def helper():\n    pass\n\n"
        "async def on_true(ctx):\n    pass\n\n"
        "class on_fall:\n    pass\n"
    )
    assert discover_python_hooks(tmp_path) == frozenset({"check"})


def test_discover_without_watch_py(tmp_path):
    assert discover_python_hooks(tmp_path) == frozenset()


def test_resolve_hooks_reports_syntax_error_and_conflicts(tmp_path):
    (tmp_path / "watch.py").write_text("def check(ctx):\n    return (\n")
    hooks, problem = resolve_hooks(tmp_path, {"on_true"})
    assert hooks == frozenset({"on_true"})
    assert problem.startswith("watch.py line 2: syntax error")

    (tmp_path / "watch.py").write_text("def check(ctx):\n    return True\n")
    hooks, problem = resolve_hooks(tmp_path, {"check", "on_true"})
    assert hooks == frozenset({"check", "on_true"})
    assert problem == "defined both in watch.py and in [hooks] of config.toml: check (keep one)"

    hooks, problem = resolve_hooks(tmp_path, {"on_true"})
    assert (hooks, problem) == (frozenset({"check", "on_true"}), None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config_watch.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.config'`

- [ ] **Step 3: Implement** — `src/keepwatch/hooks.py`

```python
"""Hook names, and discovery of Python hooks in watch.py without importing it. Stdlib only."""

from __future__ import annotations

import ast
from collections.abc import Collection
from pathlib import Path

CHECK = "check"
ACTION_HOOKS = ("on_rise", "on_fall", "on_true", "on_false")
HOOK_NAMES = (CHECK, *ACTION_HOOKS)
WATCH_PY = "watch.py"
NO_CHECK = "the watch has no check: define check(ctx) in watch.py or set check in [hooks] of config.toml"


def discover_python_hooks(watch_dir: Path) -> frozenset[str]:
    """Names of hook functions defined at the top level of watch.py.

    Returns an empty set when there is no watch.py. Raises SyntaxError or
    ValueError when watch.py cannot be parsed.
    """
    source = watch_dir / WATCH_PY
    if not source.is_file():
        return frozenset()
    tree = ast.parse(source.read_bytes(), filename=str(source))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HOOK_NAMES
    )


def resolve_hooks(watch_dir: Path, command_hooks: Collection[str]) -> tuple[frozenset[str], str | None]:
    """All hooks a watch defines, plus a problem message if its definition is broken."""
    commands = frozenset(command_hooks)
    try:
        python = discover_python_hooks(watch_dir)
    except SyntaxError as exc:
        return commands, f"{WATCH_PY} line {exc.lineno}: syntax error: {exc.msg}"
    except ValueError as exc:
        return commands, f"{WATCH_PY} cannot be parsed: {exc}"
    both = python & commands
    if both:
        names = ", ".join(sorted(both))
        return python | commands, f"defined both in watch.py and in [hooks] of config.toml: {names} (keep one)"
    return python | commands, None
```

`src/keepwatch/config.py` (watch part; Task 4 appends the global part):

```python
"""Configuration files: schema, parsing and validation with line numbers."""

from __future__ import annotations

import difflib
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from keepwatch.durations import DurationError, parse_duration
from keepwatch.hooks import HOOK_NAMES

CONFIG_NAME = "config.toml"


@dataclass(frozen=True)
class ConfigProblem:
    path: Path
    line: int | None
    message: str
    topic: str = "config"

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else str(self.path)
        return f"{where}: {self.message}; see: keepwatch docs {self.topic}"


class ConfigError(Exception):
    """One or more problems in a configuration file."""

    def __init__(self, problems: list[ConfigProblem]) -> None:
        self.problems = problems
        super().__init__("\n".join(str(problem) for problem in problems))


@dataclass(frozen=True)
class Command:
    """A command hook: argv runs directly, shell runs via /bin/sh -c."""

    argv: tuple[str, ...] | None = None
    shell: str | None = None

    def to_argv(self) -> list[str]:
        if self.shell is not None:
            return ["/bin/sh", "-c", self.shell]
        return list(self.argv or ())

    def display(self) -> str:
        return self.shell if self.shell is not None else shlex.join(self.argv or ())


@dataclass(frozen=True)
class ExitCodes:
    true: tuple[int, ...] = (0,)
    false: tuple[int, ...] = (1,)
    unknown: tuple[int, ...] = ()


@dataclass(frozen=True)
class Key:
    """One scalar config key. `doc` is the source for `keepwatch docs config`."""

    name: str
    kind: str
    default: Any
    doc: str
    defaultable: bool = False


WATCH_KEYS = (
    Key("description", "str", "", "One line shown in `status` and logs."),
    Key("enabled", "bool", True, "`false` parks the watch: loaded and validated, never polled."),
    Key(
        "interval",
        "interval",
        60.0,
        "Wait between the end of one poll and the start of the next. Minimum 1s.",
        True,
    ),
    Key(
        "initial_condition",
        "bool",
        False,
        "The condition before the first answer after the service starts.",
        True,
    ),
    Key("check_timeout", "duration", 60.0, "Deadline for the check.", True),
    Key("action_timeout", "duration", 60.0, "Deadline for each action.", True),
    Key(
        "max_failures",
        "int",
        5,
        "Consecutive failed polls before the watch goes offline. 0 disables the limit.",
        True,
    ),
    Key("retry_after", "duration", None, "While offline, make one trial poll this often.", True),
    Key("python_dependencies", "str_list", (), "PEP 508 requirements for watch.py, installed by uv."),
)

WATCH_TABLES = {
    "hooks": "Hooks implemented as commands: hook name = string (run via /bin/sh -c) or list (run directly).",
    "check_exit_codes": "Exit-code mapping for a command check: true, false, unknown (lists of 0-255).",
    "environment": "Extra environment variables for this watch's hooks.",
    "settings": "Free-form plugin parameters: ctx.settings in Python, KEEPWATCH_SETTING_* for commands.",
}


@dataclass(frozen=True)
class WatchConfig:
    name: str
    watch_dir: Path
    description: str = ""
    enabled: bool = True
    interval: float = 60.0
    initial_condition: bool = False
    check_timeout: float = 60.0
    action_timeout: float = 60.0
    max_failures: int = 5
    retry_after: float | None = None
    python_dependencies: tuple[str, ...] = ()
    hooks: Mapping[str, Command] = field(default_factory=dict)
    exit_codes: ExitCodes = ExitCodes()
    environment: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def config_file(self) -> Path:
        return self.watch_dir / CONFIG_NAME


_INVALID = object()
_HEADER = re.compile(r"^\s*\[\s*(?P<name>[^\]\s]+)\s*\]\s*(#.*)?$")


class _Bad(Exception):
    """Internal: a value has the wrong type or range."""


class _Collector:
    """Collects problems for one file and finds the line a key is on."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.lines = text.splitlines()
        self.problems: list[ConfigProblem] = []

    def add(self, message: str, *, key: str | None = None, table: str | None = None, topic: str = "config") -> None:
        self.problems.append(ConfigProblem(self.path, self.line_of(key, table), message, topic))

    def line_of(self, key: str | None, table: str | None = None) -> int | None:
        if key is None and table is None:
            return None
        key_pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""") if key else None
        in_table = table is None
        for number, line in enumerate(self.lines, start=1):
            header = _HEADER.match(line)
            if header:
                name = header.group("name")
                if key is None and name == table:
                    return number
                in_table = table is not None and name == table
                continue
            if key_pattern is not None and in_table and key_pattern.match(line):
                return number
        return None


def _read(path: Path) -> tuple[dict[str, Any], _Collector]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([ConfigProblem(path, None, f"cannot read file: {exc.strerror or exc}")]) from exc
    collector = _Collector(path, text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        found = re.search(r"line (\d+)", str(exc))
        line = int(found.group(1)) if found else None
        raise ConfigError([ConfigProblem(path, line, f"invalid TOML: {exc}")]) from exc
    return data, collector


def _convert(collector: _Collector, key: Key, value: Any, table: str | None = None) -> Any:
    try:
        if key.kind == "str":
            if not isinstance(value, str):
                raise _Bad("a string")
            return value
        if key.kind == "bool":
            if not isinstance(value, bool):
                raise _Bad("true or false")
            return value
        if key.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _Bad("a whole number >= 0")
            return value
        if key.kind in ("duration", "interval"):
            seconds = parse_duration(value)
            if key.kind == "interval" and seconds < 1:
                raise _Bad("at least 1s")
            return seconds
        if key.kind == "str_list":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise _Bad("a list of strings")
            return tuple(value)
    except DurationError as exc:
        collector.add(f"'{key.name}': {exc}", key=key.name, table=table)
        return _INVALID
    except _Bad as exc:
        collector.add(f"'{key.name}' must be {exc}, got {value!r}", key=key.name, table=table)
        return _INVALID
    raise AssertionError(f"unknown key kind {key.kind}")


def _unknown_key(collector: _Collector, name: str, valid: Iterable[str], table: str | None = None) -> None:
    close = difflib.get_close_matches(name, list(valid), n=1)
    hint = f" (did you mean '{close[0]}'?)" if close else ""
    where = f" in [{table}]" if table else ""
    collector.add(f"unknown key '{name}'{where}{hint}", key=name, table=table)


def _command(collector: _Collector, value: Any, *, key: str, table: str | None, topic: str) -> Command | None:
    if isinstance(value, str) and value.strip():
        return Command(shell=value)
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return Command(argv=tuple(value))
    collector.add(
        f"'{key}' must be a command: a non-empty string (run via /bin/sh -c) "
        f"or a non-empty list of strings (run directly), got {value!r}",
        key=key,
        table=table,
        topic=topic,
    )
    return None


def _string_table(collector: _Collector, value: Any, table: str) -> dict[str, str]:
    if not isinstance(value, dict):
        collector.add(f"'{table}' must be a table ([{table}])", key=table)
        return {}
    result = {}
    for name, raw in value.items():
        if isinstance(raw, str):
            result[name] = raw
        else:
            collector.add(f"'{name}' must be a string, got {raw!r}", key=name, table=table)
    return result


def _hooks_table(collector: _Collector, value: Any) -> dict[str, Command]:
    if not isinstance(value, dict):
        collector.add("'hooks' must be a table ([hooks])", key="hooks", topic="executables")
        return {}
    hooks = {}
    for name, raw in value.items():
        if name not in HOOK_NAMES:
            _unknown_key(collector, name, HOOK_NAMES, table="hooks")
            continue
        command = _command(collector, raw, key=name, table="hooks", topic="executables")
        if command is not None:
            hooks[name] = command
    return hooks


def _exit_codes_table(collector: _Collector, value: Any) -> ExitCodes:
    table = "check_exit_codes"
    if not isinstance(value, dict):
        collector.add(f"'{table}' must be a table ([{table}])", key=table, topic="executables")
        return ExitCodes()
    lists: dict[str, tuple[int, ...]] = {"true": (0,), "false": (1,), "unknown": ()}
    for name, raw in value.items():
        if name not in lists:
            _unknown_key(collector, name, lists, table=table)
            continue
        if not isinstance(raw, list) or not all(
            isinstance(code, int) and not isinstance(code, bool) and 0 <= code <= 255 for code in raw
        ):
            collector.add(f"'{name}' must be a list of exit codes (0-255), got {raw!r}", key=name, table=table)
            continue
        lists[name] = tuple(raw)
    seen: dict[int, str] = {}
    for name, codes in lists.items():
        for code in codes:
            if code in seen:
                collector.add(f"exit code {code} is listed under both '{seen[code]}' and '{name}'", table=table)
            seen[code] = name
    return ExitCodes(**lists)


def load_watch_config(watch_dir: Path, defaults: Mapping[str, Any] | None = None) -> WatchConfig:
    """Read and validate <watch_dir>/config.toml. Raises ConfigError listing every problem."""
    path = watch_dir / CONFIG_NAME
    if not path.is_file():
        raise ConfigError([ConfigProblem(path, None, "missing config.toml (every watch needs one, even if empty)")])
    data, collector = _read(path)
    by_name = {key.name: key for key in WATCH_KEYS}
    values: dict[str, Any] = {key.name: key.default for key in WATCH_KEYS}
    values.update(defaults or {})
    hooks: dict[str, Command] = {}
    exit_codes = ExitCodes()
    environment: dict[str, str] = {}
    settings: dict[str, Any] = {}
    for name, value in data.items():
        if name in by_name:
            converted = _convert(collector, by_name[name], value)
            if converted is not _INVALID:
                values[name] = converted
        elif name == "hooks":
            hooks = _hooks_table(collector, value)
        elif name == "check_exit_codes":
            exit_codes = _exit_codes_table(collector, value)
        elif name == "environment":
            environment = _string_table(collector, value, "environment")
        elif name == "settings":
            if isinstance(value, dict):
                settings = value
            else:
                collector.add("'settings' must be a table ([settings])", key="settings")
        else:
            _unknown_key(collector, name, [*by_name, *WATCH_TABLES])
    if collector.problems:
        raise ConfigError(collector.problems)
    return WatchConfig(
        name=watch_dir.name,
        watch_dir=watch_dir,
        hooks=MappingProxyType(hooks),
        exit_codes=exit_codes,
        environment=MappingProxyType(environment),
        settings=MappingProxyType(settings),
        **values,
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_watch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/hooks.py src/keepwatch/config.py tests/test_config_watch.py
git commit -m "Parse and validate watch config with line-numbered problems"
```

---

### Task 4: Global config and watch discovery

**Files:**
- Modify: `src/keepwatch/config.py` (append; add `import os` and the `Paths` import at the top)
- Test: `tests/test_config_global.py`

**Interfaces:**
- Consumes: `Paths` (Task 2), everything in Task 3.
- Produces:
  - `LogSettings(max_bytes=10_000_000, backups=10, capture_bytes=65_536)`
  - `GlobalConfig(path: Path, watch_dirs: tuple[Path, ...], reload_interval=5.0, alert_command: Command | None = None, defaults: Mapping[str, Any] = {}, environment: Mapping[str, str] = {}, log: LogSettings = LogSettings())`
  - `GLOBAL_KEYS`, `GLOBAL_TABLES`, `LOG_KEYS`
  - `load_global_config(path: Path, paths: Paths) -> GlobalConfig`
  - `Discovery(watches: Mapping[str, Path], problems: tuple[ConfigProblem, ...])`; `discover_watches(global_config: GlobalConfig) -> Discovery`
  - `WatchNotFound(Exception)`; `find_watch(discovery: Discovery, name: str) -> Path`

- [ ] **Step 1: Write the failing tests** — `tests/test_config_global.py`

```python
import os
import textwrap
from pathlib import Path

import pytest

from keepwatch.config import (
    Command,
    ConfigError,
    GlobalConfig,
    LogSettings,
    WatchNotFound,
    discover_watches,
    find_watch,
    load_global_config,
    load_watch_config,
)


def write_global(xdg, text):
    xdg.config_home.mkdir(parents=True, exist_ok=True)
    xdg.config_file.write_text(textwrap.dedent(text))


def test_missing_global_config_uses_defaults(xdg):
    cfg = load_global_config(xdg.config_file, xdg)
    assert cfg.watch_dirs == (xdg.default_watches_dir,)
    assert cfg.reload_interval == 5.0
    assert cfg.alert_command is None
    assert cfg.log == LogSettings()
    assert dict(cfg.defaults) == {} and dict(cfg.environment) == {}


def test_full_global_config(xdg, tmp_path, monkeypatch):
    monkeypatch.setenv("KW_TEST_DIR", str(tmp_path / "x"))
    write_global(xdg, '''
        watch_dirs = ["~/w1", "more", "$KW_TEST_DIR/w3"]
        reload_interval = "10s"
        alert_command = ["notify-send", "keepwatch"]

        [defaults]
        interval = "2m"
        max_failures = 8

        [environment]
        SSH_AUTH_SOCK = "/tmp/agent"

        [log]
        max_bytes = 5000
        backups = 2
        capture_bytes = 1024
    ''')
    cfg = load_global_config(xdg.config_file, xdg)
    home = Path(os.environ["HOME"])
    assert cfg.watch_dirs == (home / "w1", xdg.config_home / "more", tmp_path / "x" / "w3")
    assert cfg.reload_interval == 10.0
    assert cfg.alert_command == Command(argv=("notify-send", "keepwatch"))
    assert dict(cfg.defaults) == {"interval": 120.0, "max_failures": 8}
    assert dict(cfg.environment) == {"SSH_AUTH_SOCK": "/tmp/agent"}
    assert cfg.log == LogSettings(max_bytes=5000, backups=2, capture_bytes=1024)


def test_defaults_rejects_non_defaultable_key(xdg):
    write_global(xdg, '[defaults]\ndescription = "x"\n')
    with pytest.raises(ConfigError) as info:
        load_global_config(xdg.config_file, xdg)
    (problem,) = info.value.problems
    assert problem.line == 2
    assert problem.message.startswith("'description' cannot be set in [defaults]")


def test_global_unknown_key_suggests(xdg):
    write_global(xdg, "watch_dir = []\n")
    with pytest.raises(ConfigError, match="did you mean 'watch_dirs'"):
        load_global_config(xdg.config_file, xdg)


def test_watch_dirs_must_be_a_nonempty_list(xdg):
    write_global(xdg, "watch_dirs = []\n")
    with pytest.raises(ConfigError, match="'watch_dirs' must be a non-empty list"):
        load_global_config(xdg.config_file, xdg)


def test_log_values_must_be_positive(xdg):
    write_global(xdg, "[log]\nmax_bytes = 0\n")
    with pytest.raises(ConfigError, match="'max_bytes' must be at least 1"):
        load_global_config(xdg.config_file, xdg)


def test_defaults_flow_into_watch_config(xdg, make_watch):
    write_global(xdg, '[defaults]\ninterval = "2m"\n')
    watch_dir = make_watch("w")
    cfg = load_global_config(xdg.config_file, xdg)
    assert load_watch_config(watch_dir, cfg.defaults).interval == 120.0


def test_discover_watches(xdg, tmp_path, make_watch):
    second = tmp_path / "second"
    make_watch("a")
    make_watch("dup")
    make_watch("dup", base=second)
    make_watch("b", base=second)
    (xdg.default_watches_dir / ".git").mkdir()
    (xdg.default_watches_dir / "_parked").mkdir()
    (xdg.default_watches_dir / "notes.txt").write_text("")
    (xdg.default_watches_dir / "bad name").mkdir()
    cfg = GlobalConfig(path=xdg.config_file, watch_dirs=(xdg.default_watches_dir, second, tmp_path / "missing"))
    found = discover_watches(cfg)
    assert list(found.watches) == ["a", "dup", "b"]
    assert found.watches["dup"] == xdg.default_watches_dir / "dup"
    messages = [problem.message for problem in found.problems]
    assert any(message.startswith("duplicate watch name 'dup'") for message in messages)
    assert any("does not exist" in message for message in messages)
    assert any("may contain only" in message for message in messages)
    assert len(messages) == 3


def test_find_watch_suggests_a_close_name(xdg, make_watch):
    make_watch("psg-export")
    found = discover_watches(load_global_config(xdg.config_file, xdg))
    assert find_watch(found, "psg-export") == xdg.default_watches_dir / "psg-export"
    with pytest.raises(WatchNotFound, match="did you mean 'psg-export'"):
        find_watch(found, "psg-exprot")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config_global.py -q`
Expected: FAIL with `ImportError: cannot import name 'GlobalConfig'`

- [ ] **Step 3: Implement** — at the top of `src/keepwatch/config.py` add `import os` to the stdlib imports and `from keepwatch.paths import Paths` after the `keepwatch.hooks` import. Then append:

```python
@dataclass(frozen=True)
class LogSettings:
    max_bytes: int = 10_000_000
    backups: int = 10
    capture_bytes: int = 65_536


@dataclass(frozen=True)
class GlobalConfig:
    path: Path
    watch_dirs: tuple[Path, ...]
    reload_interval: float = 5.0
    alert_command: Command | None = None
    defaults: Mapping[str, Any] = field(default_factory=dict)
    environment: Mapping[str, str] = field(default_factory=dict)
    log: LogSettings = LogSettings()


GLOBAL_KEYS = (
    Key(
        "watch_dirs",
        "path_list",
        None,
        "Directories whose subdirectories are watches. Earlier entries win name clashes. "
        "Default: $XDG_CONFIG_HOME/keepwatch/watches.",
    ),
    Key("reload_interval", "interval", 5.0, "Master tick: how often config changes are picked up."),
    Key("alert_command", "command", None, "Run when a watch goes offline or comes back online."),
)

GLOBAL_TABLES = {
    "defaults": "Default values for defaultable watch keys.",
    "environment": "Extra environment variables for every hook.",
    "log": "Log rotation and output capture limits.",
}

LOG_KEYS = (
    Key("max_bytes", "int", 10_000_000, "Rotate the log at this size."),
    Key("backups", "int", 10, "Rotated log files kept."),
    Key(
        "capture_bytes",
        "int",
        65_536,
        "Per stream (stdout, stderr) captured into a log record; longer output keeps head and tail.",
    ),
)


def _watch_dirs(collector: _Collector, value: Any, base: Path) -> tuple[Path, ...] | None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        collector.add(f"'watch_dirs' must be a non-empty list of directory paths, got {value!r}", key="watch_dirs")
        return None
    dirs = []
    for item in value:
        expanded = Path(os.path.expandvars(os.path.expanduser(item)))
        dirs.append(expanded if expanded.is_absolute() else base / expanded)
    return tuple(dirs)


def _defaults_table(collector: _Collector, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        collector.add("'defaults' must be a table ([defaults])", key="defaults")
        return {}
    by_name = {key.name: key for key in WATCH_KEYS}
    allowed = [key.name for key in WATCH_KEYS if key.defaultable]
    defaults = {}
    for name, raw in value.items():
        if name not in by_name:
            _unknown_key(collector, name, allowed, table="defaults")
            continue
        if not by_name[name].defaultable:
            collector.add(
                f"'{name}' cannot be set in [defaults]; defaultable keys: {', '.join(allowed)}",
                key=name,
                table="defaults",
            )
            continue
        converted = _convert(collector, by_name[name], raw, table="defaults")
        if converted is not _INVALID:
            defaults[name] = converted
    return defaults


def _log_table(collector: _Collector, value: Any) -> LogSettings:
    if not isinstance(value, dict):
        collector.add("'log' must be a table ([log])", key="log")
        return LogSettings()
    by_name = {key.name: key for key in LOG_KEYS}
    values = {}
    for name, raw in value.items():
        if name not in by_name:
            _unknown_key(collector, name, by_name, table="log")
            continue
        converted = _convert(collector, by_name[name], raw, table="log")
        if converted is _INVALID:
            continue
        if converted < 1:
            collector.add(f"'{name}' must be at least 1, got {raw!r}", key=name, table="log")
            continue
        values[name] = converted
    return LogSettings(**values)


def load_global_config(path: Path, paths: Paths) -> GlobalConfig:
    """Read and validate the global config. A missing file means all defaults."""
    if not path.exists():
        return GlobalConfig(path=path, watch_dirs=(paths.default_watches_dir,))
    data, collector = _read(path)
    by_name = {key.name: key for key in GLOBAL_KEYS}
    watch_dirs: tuple[Path, ...] | None = (paths.default_watches_dir,)
    reload_interval = 5.0
    alert_command = None
    defaults: dict[str, Any] = {}
    environment: dict[str, str] = {}
    log = LogSettings()
    for name, value in data.items():
        if name == "watch_dirs":
            watch_dirs = _watch_dirs(collector, value, path.parent)
        elif name == "reload_interval":
            converted = _convert(collector, by_name[name], value)
            if converted is not _INVALID:
                reload_interval = converted
        elif name == "alert_command":
            alert_command = _command(collector, value, key=name, table=None, topic="failures")
        elif name == "defaults":
            defaults = _defaults_table(collector, value)
        elif name == "environment":
            environment = _string_table(collector, value, "environment")
        elif name == "log":
            log = _log_table(collector, value)
        else:
            _unknown_key(collector, name, [*by_name, *GLOBAL_TABLES])
    if collector.problems or watch_dirs is None:
        raise ConfigError(collector.problems)
    return GlobalConfig(
        path=path,
        watch_dirs=watch_dirs,
        reload_interval=reload_interval,
        alert_command=alert_command,
        defaults=MappingProxyType(defaults),
        environment=MappingProxyType(environment),
        log=log,
    )


_WATCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class Discovery:
    watches: Mapping[str, Path]
    problems: tuple[ConfigProblem, ...]


def discover_watches(global_config: GlobalConfig) -> Discovery:
    """Find watch directories in watch_dirs order. Earlier directories win duplicate names."""
    found: dict[str, Path] = {}
    problems: list[ConfigProblem] = []
    for base in global_config.watch_dirs:
        if not base.is_dir():
            problems.append(ConfigProblem(base, None, "watches directory does not exist (create it or fix watch_dirs)"))
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith((".", "_")):
                continue
            if not _WATCH_NAME.fullmatch(entry.name):
                problems.append(
                    ConfigProblem(entry, None, "watch directory names may contain only letters, digits, '_', '.' and '-'")
                )
                continue
            if entry.name in found:
                problems.append(
                    ConfigProblem(
                        entry,
                        None,
                        f"duplicate watch name '{entry.name}': {found[entry.name]} wins "
                        "(it comes first in watch_dirs); this one is skipped",
                    )
                )
                continue
            found[entry.name] = entry
    return Discovery(MappingProxyType(found), tuple(problems))


class WatchNotFound(Exception):
    """No watch with the requested name."""


def find_watch(discovery: Discovery, name: str) -> Path:
    if name in discovery.watches:
        return discovery.watches[name]
    close = difflib.get_close_matches(name, list(discovery.watches), n=1)
    hint = f" (did you mean '{close[0]}'?)" if close else ""
    known = ", ".join(discovery.watches) or "none"
    raise WatchNotFound(f"no watch named '{name}'{hint}; known watches: {known}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_config_global.py tests/test_config_watch.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/config.py tests/test_config_global.py
git commit -m "Add global config, defaults inheritance and watch discovery"
```

---

### Task 5: State machine and backoff

**Files:**
- Create: `src/keepwatch/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `Outcome(StrEnum)`: `TRUE="true"`, `FALSE="false"`, `UNKNOWN="unknown"`, `TIMEOUT="timeout"`, `ERROR="error"`; `ANSWERS`, `CHECK_FAILURES` (frozensets)
  - `Edge(StrEnum)`: `RISE="rise"`, `FALL="fall"`; `EDGE_HOOKS = {Edge.RISE: "on_rise", Edge.FALL: "on_fall"}`
  - `WatchState(condition: bool, pending_edge: Edge | None = None, failures: int = 0)` (frozen)
  - `initial_state(initial_condition: bool) -> WatchState`
  - `Plan(state: WatchState, outcome: Outcome, actions: tuple[str, ...], check_failed: bool)` (frozen)
  - `plan_poll(state: WatchState, outcome: Outcome, defined_hooks: Collection[str]) -> Plan`
  - `finish_poll(plan: Plan, results: Sequence[tuple[str, bool]]) -> tuple[WatchState, bool]` — new state and whether the poll failed
  - `next_delay(interval: float, failures: int) -> float`; `should_go_offline(failures: int, max_failures: int) -> bool`

- [ ] **Step 1: Write the failing tests** — `tests/test_state.py`

```python
import pytest

from keepwatch.state import (
    Edge,
    Outcome,
    WatchState,
    finish_poll,
    initial_state,
    next_delay,
    plan_poll,
    should_go_offline,
)

ALL = {"on_rise", "on_fall", "on_true", "on_false"}
T, F = Outcome.TRUE, Outcome.FALSE


@pytest.mark.parametrize(
    "before, outcome, condition, pending, actions",
    [
        (WatchState(False), T, True, Edge.RISE, ("on_rise", "on_true")),
        (WatchState(True), T, True, None, ("on_true",)),
        (WatchState(False), F, False, None, ("on_false",)),
        (WatchState(True), F, False, Edge.FALL, ("on_fall", "on_false")),
        (WatchState(True, Edge.RISE), T, True, Edge.RISE, ("on_rise", "on_true")),
        (WatchState(True, Edge.RISE), F, False, Edge.FALL, ("on_fall", "on_false")),
    ],
)
def test_answers(before, outcome, condition, pending, actions):
    plan = plan_poll(before, outcome, ALL)
    assert plan.state.condition is condition
    assert plan.state.pending_edge == pending
    assert plan.actions == actions
    assert plan.check_failed is False


@pytest.mark.parametrize("outcome, failed", [(Outcome.UNKNOWN, False), (Outcome.TIMEOUT, True), (Outcome.ERROR, True)])
def test_non_answers_hold_the_condition(outcome, failed):
    before = WatchState(True, Edge.RISE, 2)
    plan = plan_poll(before, outcome, ALL)
    assert plan.state == before
    assert plan.actions == ()
    assert plan.check_failed is failed


def test_undefined_hooks_are_skipped_and_edge_cleared():
    plan = plan_poll(WatchState(False), T, {"on_true"})
    assert plan.actions == ("on_true",)
    assert plan.state.pending_edge is None


def test_successful_edge_clears_pending_and_resets_failures():
    plan = plan_poll(WatchState(False, None, 3), T, ALL)
    state, failed = finish_poll(plan, [("on_rise", True), ("on_true", True)])
    assert state == WatchState(True, None, 0)
    assert failed is False


def test_failed_edge_stays_pending_and_counts():
    plan = plan_poll(WatchState(False), T, ALL)
    state, failed = finish_poll(plan, [("on_rise", False)])
    assert state == WatchState(True, Edge.RISE, 1)
    assert failed is True


def test_check_failure_counts():
    plan = plan_poll(WatchState(False, None, 1), Outcome.ERROR, ALL)
    state, failed = finish_poll(plan, [])
    assert state.failures == 2 and failed is True


def test_unknown_leaves_failures_unchanged():
    plan = plan_poll(WatchState(False, None, 2), Outcome.UNKNOWN, ALL)
    state, failed = finish_poll(plan, [])
    assert state.failures == 2 and failed is False


def test_initial_state():
    assert initial_state(True) == WatchState(True, None, 0)


@pytest.mark.parametrize(
    "interval, failures, delay",
    [
        (30, 0, 30),
        (30, 1, 60),
        (30, 2, 120),
        (30, 3, 240),
        (30, 4, 480),
        (600, 1, 600),
        (600, 2, 1200),
        (30, 10, 3600),
        (7200, 3, 7200),
    ],
)
def test_next_delay(interval, failures, delay):
    assert next_delay(interval, failures) == delay


def test_should_go_offline():
    assert should_go_offline(5, 5) is True
    assert should_go_offline(4, 5) is False
    assert should_go_offline(100, 0) is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_state.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.state'`

- [ ] **Step 3: Implement** — `src/keepwatch/state.py`

```python
"""The per-watch state machine (spec section 7) and backoff (section 8.2). Pure: no I/O."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum


class Outcome(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    ERROR = "error"


ANSWERS = frozenset({Outcome.TRUE, Outcome.FALSE})
CHECK_FAILURES = frozenset({Outcome.TIMEOUT, Outcome.ERROR})


class Edge(StrEnum):
    RISE = "rise"
    FALL = "fall"


EDGE_HOOKS = {Edge.RISE: "on_rise", Edge.FALL: "on_fall"}
BACKOFF_BASE = 60.0
BACKOFF_CAP = 3600.0


@dataclass(frozen=True)
class WatchState:
    condition: bool
    pending_edge: Edge | None = None
    failures: int = 0


def initial_state(initial_condition: bool) -> WatchState:
    return WatchState(condition=initial_condition)


@dataclass(frozen=True)
class Plan:
    """What a poll will do: the state after the outcome, and the actions to run in order."""

    state: WatchState
    outcome: Outcome
    actions: tuple[str, ...]
    check_failed: bool


def plan_poll(state: WatchState, outcome: Outcome, defined_hooks: Collection[str]) -> Plan:
    """Apply a check outcome to the state and list the actions to run (section 7.1)."""
    if outcome not in ANSWERS:
        return Plan(state, outcome, (), outcome in CHECK_FAILURES)
    condition = outcome is Outcome.TRUE
    pending = state.pending_edge
    if condition != state.condition:
        pending = Edge.RISE if condition else Edge.FALL
    actions: list[str] = []
    if pending is not None:
        edge_hook = EDGE_HOOKS[pending]
        if edge_hook in defined_hooks:
            actions.append(edge_hook)
        else:
            pending = None
    level_hook = "on_true" if condition else "on_false"
    if level_hook in defined_hooks:
        actions.append(level_hook)
    return Plan(replace(state, condition=condition, pending_edge=pending), outcome, tuple(actions), False)


def finish_poll(plan: Plan, results: Sequence[tuple[str, bool]]) -> tuple[WatchState, bool]:
    """Fold action results (hook, succeeded) into the state. Returns (state, poll_failed)."""
    failed = plan.check_failed or any(not succeeded for _, succeeded in results)
    pending = plan.state.pending_edge
    if pending is not None and (EDGE_HOOKS[pending], True) in results:
        pending = None
    if failed:
        failures = plan.state.failures + 1
    elif plan.outcome in ANSWERS:
        failures = 0
    else:
        failures = plan.state.failures
    return replace(plan.state, pending_edge=pending, failures=failures), failed


def next_delay(interval: float, failures: int) -> float:
    """Seconds to wait before the next poll (section 8.2)."""
    if failures <= 0:
        return interval
    base = max(interval, BACKOFF_BASE)
    cap = max(interval, BACKOFF_CAP)
    return min(base * 2 ** (failures - 1), cap)


def should_go_offline(failures: int, max_failures: int) -> bool:
    return max_failures > 0 and failures >= max_failures
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_state.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/state.py tests/test_state.py
git commit -m "Add the pure state machine and backoff arithmetic"
```

---

### Task 6: Protocol helpers, Ctx and Ledger

**Files:**
- Create: `src/keepwatch/protocol.py`
- Create: `src/keepwatch/ctx.py`
- Modify: `src/keepwatch/__init__.py`
- Test: `tests/test_protocol.py`, `tests/test_ctx.py`

**Interfaces:**
- Consumes: `parse_duration` (Task 1).
- Produces:
  - `protocol.PROTOCOL_VERSION = 1`, `MAX_PAYLOAD_BYTES = 1_048_576`
  - `encode(message: dict) -> bytes`; `decode_lines(data: bytes) -> tuple[list[dict], list[str]]`
  - `clip(text: str, limit: int) -> tuple[str, bool]`
  - `normalize_payload(payload: Any) -> tuple[Any, str | None]` — JSON-safe copy (paths → str, tuples → lists) or a problem
  - `ctx.Unknown(reason="")` (`.reason`); `CommandFailed(argv, returncode, stderr)`; `LedgerReadOnly`
  - `Ledger(path: Path, *, expire: float | None = None, writable: bool = True)` with `in`, `len`, iteration (sorted keys), `added_at(key) -> datetime | None`, `add(key)`, `discard(key)`
  - `Ctx(*, watch, hook, poll_id, condition, payload, settings, watch_dir, data_dir, run_dir, deadline: float (epoch seconds), capture_bytes=65_536, emit: Callable[[dict], None] | None = None)` with attributes `watch, hook, poll_id, condition, payload, settings, watch_dir, log`, properties `data_dir`, `run_dir`, methods `run(...)`, `ledger(name, expire=None)`, `file_key(path)`, `glob(pattern)`, `unchanged_for(path, duration)`
  - `Ctx.run` emits `{"type": "command", "argv", "shell", "exit_code", "timed_out", "duration", "stdout", "stderr", "stdout_truncated", "stderr_truncated"}`
  - `keepwatch` package exports `Ctx, Ledger, Unknown, CommandFailed, LedgerReadOnly, __version__`

- [ ] **Step 1: Write the failing tests** — `tests/test_protocol.py`

```python
from pathlib import Path

from keepwatch.protocol import MAX_PAYLOAD_BYTES, clip, decode_lines, encode, normalize_payload


def test_encode_and_decode_round_trip():
    data = encode({"type": "log", "message": "héllo"}) + b"not json\n" + encode({"type": "result"})
    messages, bad = decode_lines(data)
    assert messages == [{"type": "log", "message": "héllo"}, {"type": "result"}]
    assert bad == ["not json"]


def test_clip_keeps_head_and_tail():
    text, cut = clip("a" * 50 + "b" * 50, 20)
    assert cut is True
    assert text.startswith("a" * 10) and text.endswith("b" * 10)
    assert "80 characters omitted" in text
    assert clip("short", 20) == ("short", False)


def test_normalize_payload_converts_paths_and_tuples():
    value, problem = normalize_payload({"files": (Path("/in/a.tar.gz"), "b")})
    assert problem is None
    assert value == {"files": ["/in/a.tar.gz", "b"]}


def test_normalize_payload_rejects_unserializable_and_oversized():
    assert normalize_payload({1, 2})[1] == "payload is not JSON-serializable: set is not JSON-serializable"
    value, problem = normalize_payload("x" * MAX_PAYLOAD_BYTES)
    assert value is None and problem.startswith("payload is ")
```

`tests/test_ctx.py`:

```python
import os
import subprocess
import time
from pathlib import Path

import pytest

from keepwatch import CommandFailed, Ctx, Ledger, LedgerReadOnly


def make_ctx(tmp_path, hook="on_true", emitted=None, deadline_in=30.0):
    watch_dir = tmp_path / "w"
    watch_dir.mkdir(exist_ok=True)
    return Ctx(
        watch="w",
        hook=hook,
        poll_id="p1",
        condition=True,
        payload=None,
        settings={"dest": "x"},
        watch_dir=watch_dir,
        data_dir=tmp_path / "data",
        run_dir=tmp_path / "run",
        deadline=time.time() + deadline_in,
        capture_bytes=1000,
        emit=(emitted.append if emitted is not None else None),
    )


def test_ledger_persists_and_writes_atomically(tmp_path):
    path = tmp_path / "ledgers" / "sent.json"
    ledger = Ledger(path)
    ledger.add("a")
    ledger.add("b")
    ledger.discard("a")
    reopened = Ledger(path)
    assert "b" in reopened and "a" not in reopened
    assert list(reopened) == ["b"] and len(reopened) == 1
    assert reopened.added_at("b") is not None
    assert [p.name for p in path.parent.iterdir()] == ["sent.json"]


def test_ledger_expiry(tmp_path, monkeypatch):
    path = tmp_path / "sent.json"
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    ledger = Ledger(path, expire=60)
    ledger.add("old")
    now[0] += 61
    assert "old" not in ledger
    ledger.add("new")
    assert '"old"' not in path.read_text()


def test_ledger_read_only_and_key_type(tmp_path):
    ledger = Ledger(tmp_path / "x.json", writable=False)
    with pytest.raises(LedgerReadOnly):
        ledger.add("k")
    with pytest.raises(TypeError):
        Ledger(tmp_path / "y.json").add(Path("/k"))


def test_ctx_ledger_is_read_only_in_check(tmp_path):
    ctx = make_ctx(tmp_path, hook="check")
    with pytest.raises(LedgerReadOnly):
        ctx.ledger("sent").add("k")
    make_ctx(tmp_path, hook="on_true").ledger("sent", expire="90d").add("k")
    assert "k" in make_ctx(tmp_path, hook="check").ledger("sent")
    with pytest.raises(ValueError):
        make_ctx(tmp_path).ledger("../escape")


def test_file_key_changes_with_mtime(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "f.tar.gz"
    target.write_text("data")
    first = ctx.file_key(target)
    os.utime(target, (1, 1))
    assert ctx.file_key(target) != first
    assert first.startswith(f"{target}|4|")


def test_glob_expands_home_and_sorts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("b.tar.gz", "a.tar.gz", "c.txt"):
        (tmp_path / name).write_text("")
    assert make_ctx(tmp_path).glob("~/*.tar.gz") == [tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"]


def test_unchanged_for(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "f"
    target.write_text("x")
    assert ctx.unchanged_for(target, "30s") is False
    old = time.time() - 60
    os.utime(target, (old, old))
    assert ctx.unchanged_for(target, "30s") is True
    assert ctx.unchanged_for(tmp_path / "missing", "30s") is False


def test_run_captures_and_reports(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted)
    completed = ctx.run(["sh", "-c", "echo out; echo err >&2"])
    assert completed.stdout == "out\n"
    (record,) = emitted
    assert record["type"] == "command"
    assert record["exit_code"] == 0 and record["stdout"] == "out\n" and record["stderr"] == "err\n"
    assert record["timed_out"] is False and record["shell"] is False


def test_run_raises_on_failure_with_stderr_tail(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(CommandFailed, match="exited 3.*nope"):
        ctx.run("echo nope >&2; exit 3")
    assert ctx.run("exit 3", check=False).returncode == 3


def test_run_stdin_is_empty_and_input_works(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.run(["cat"]).stdout == ""
    assert ctx.run(["cat"], input="hi").stdout == "hi"


def test_run_times_out_at_the_hook_deadline(tmp_path):
    emitted = []
    ctx = make_ctx(tmp_path, emitted=emitted, deadline_in=0.5)
    with pytest.raises(subprocess.TimeoutExpired):
        ctx.run(["sleep", "10"])
    assert emitted[0]["timed_out"] is True


def test_dirs_created_on_access(tmp_path):
    ctx = make_ctx(tmp_path)
    assert not (tmp_path / "data").exists()
    assert ctx.data_dir.is_dir() and ctx.run_dir.is_dir()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_protocol.py tests/test_ctx.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.protocol'`

- [ ] **Step 3: Implement** — `src/keepwatch/protocol.py`

```python
"""Messages between the service and a worker process (JSON lines). Stdlib only."""

from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any

PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 1024 * 1024


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def decode_lines(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSON lines. Returns (messages, lines that were not JSON objects)."""
    messages: list[dict[str, Any]] = []
    bad: list[str] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            bad.append(raw)
            continue
        if isinstance(value, dict):
            messages.append(value)
        else:
            bad.append(raw)
    return messages, bad


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Keep at most about `limit` characters: the head and the tail, with a marker between."""
    if len(text) <= limit:
        return text, False
    half = max(limit // 2, 1)
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n…[{omitted} characters omitted]…\n{text[-half:]}", True


def _payload_default(value: Any) -> Any:
    if isinstance(value, PurePath):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON-serializable")


def normalize_payload(payload: Any) -> tuple[Any, str | None]:
    """Return a JSON-safe copy of payload (paths become strings), or (None, problem)."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_payload_default)
    except (TypeError, ValueError) as exc:
        return None, f"payload is not JSON-serializable: {exc}"
    size = len(encoded.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        return None, f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES} (1 MiB)"
    return json.loads(encoded), None
```

`src/keepwatch/ctx.py`:

```python
"""The API a watch's Python hooks receive. Stdlib only: this runs inside plugin environments.

Every public member is documented here; `keepwatch docs ctx` is generated from these docstrings.
"""

from __future__ import annotations

import contextlib
import glob as _glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from keepwatch.durations import parse_duration
from keepwatch.protocol import clip

Emit = Callable[[dict[str, Any]], None]
_LEDGER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class Unknown(Exception):
    """Raise from check() to answer "unknown" with a reason that goes into the log."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class CommandFailed(Exception):
    """Raised by Ctx.run() when a command exits nonzero and check=True."""

    def __init__(self, argv: Sequence[str] | str, returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        shown = argv if isinstance(argv, str) else " ".join(argv)
        tail = stderr.strip().splitlines()[-5:]
        detail = f"; stderr: {' | '.join(tail)}" if tail else ""
        super().__init__(f"command exited {returncode}: {shown}{detail}")


class LedgerReadOnly(Exception):
    """Raised when a check tries to change a ledger. Checks must only observe."""


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class Ledger:
    """A persistent set of string keys, each stamped with the time it was added.

    Stored as JSON and rewritten atomically on every add() and discard(), so a
    crash mid-loop keeps everything recorded so far. With `expire` (seconds),
    entries older than that are treated as absent and pruned on the next write.
    Beware: if the thing a key stands for can still be seen after its entry
    expires, the watch will act on it again.
    """

    def __init__(self, path: Path, *, expire: float | None = None, writable: bool = True) -> None:
        self.path = path
        self.expire = expire
        self.writable = writable
        self._entries = self._load()

    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        return {str(key): float(stamp) for key, stamp in entries.items()}

    def _live(self) -> dict[str, float]:
        if self.expire is None:
            return self._entries
        cutoff = time.time() - self.expire
        return {key: stamp for key, stamp in self._entries.items() if stamp >= cutoff}

    def __contains__(self, key: object) -> bool:
        return key in self._live()

    def __len__(self) -> int:
        return len(self._live())

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._live()))

    def added_at(self, key: str) -> datetime | None:
        """When key was added (UTC), or None if it is absent or expired."""
        stamp = self._live().get(key)
        return None if stamp is None else datetime.fromtimestamp(stamp, tz=timezone.utc)

    def add(self, key: str) -> None:
        """Add key (or refresh its timestamp) and save immediately."""
        self._check_writable()
        if not isinstance(key, str):
            raise TypeError(f"ledger keys must be strings, got {type(key).__name__}")
        self._entries[key] = time.time()
        self._save()

    def discard(self, key: str) -> None:
        """Remove key if present and save immediately."""
        self._check_writable()
        if self._entries.pop(key, None) is not None:
            self._save()

    def _check_writable(self) -> None:
        if not self.writable:
            raise LedgerReadOnly(f"{self.path.name}: a check must not change a ledger; do it in an action")

    def _save(self) -> None:
        self._entries = dict(self._live())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "entries": self._entries}, handle, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)
            raise


class Ctx:
    """Everything a hook receives. See `keepwatch docs ctx`.

    Attributes: watch (name), hook (which hook is running), poll_id (on every
    log record of this poll), condition (before the answer in check, after it
    in actions), payload (what check returned with its answer; actions only),
    settings (the [settings] table), watch_dir (also the working directory),
    log (a logging.Logger whose records land in keepwatch's log).
    """

    def __init__(
        self,
        *,
        watch: str,
        hook: str,
        poll_id: str,
        condition: bool,
        payload: Any,
        settings: Mapping[str, Any],
        watch_dir: Path,
        data_dir: Path,
        run_dir: Path,
        deadline: float,
        capture_bytes: int = 65_536,
        emit: Emit | None = None,
    ) -> None:
        self.watch = watch
        self.hook = hook
        self.poll_id = poll_id
        self.condition = condition
        self.payload = payload
        self.settings = settings
        self.watch_dir = watch_dir
        self.log = logging.getLogger(f"keepwatch.watch.{watch}")
        self._data_dir = data_dir
        self._run_dir = run_dir
        self._deadline = deadline
        self._capture_bytes = capture_bytes
        self._emit = emit or (lambda message: None)

    @property
    def data_dir(self) -> Path:
        """Persistent storage for this watch; created on first access."""
        self._data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._data_dir

    @property
    def run_dir(self) -> Path:
        """Run-only scratch space, deleted when the keepwatch process exits; created on first access."""
        self._run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._run_dir

    def _remaining(self) -> float:
        return max(self._deadline - time.time(), 0.0)

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]] | str,
        *,
        timeout: float | None = None,
        check: bool = True,
        input: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and log it (command, exit code, duration, output).

        argv: a list runs directly; a string runs via /bin/sh -c. stdin is
        /dev/null unless `input` is given. `timeout` defaults to (and is capped
        at) the hook's remaining time; on expiry subprocess.TimeoutExpired is
        raised. With check=True a nonzero exit raises CommandFailed.
        """
        shell = isinstance(argv, str)
        args: str | list[str] = argv if isinstance(argv, str) else [os.fspath(item) for item in argv]
        remaining = self._remaining()
        limit = remaining if timeout is None else min(timeout, remaining)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                args,
                shell=shell,
                input=input,
                stdin=subprocess.DEVNULL if input is None else None,
                capture_output=True,
                text=True,
                timeout=limit,
                env={**os.environ, **(env or {})},
                cwd=cwd if cwd is not None else self.watch_dir,
            )
        except subprocess.TimeoutExpired as exc:
            self._report(args, shell, None, True, time.monotonic() - started, _as_text(exc.stdout), _as_text(exc.stderr))
            raise
        self._report(args, shell, completed.returncode, False, time.monotonic() - started, completed.stdout, completed.stderr)
        if check and completed.returncode != 0:
            raise CommandFailed(args, completed.returncode, completed.stderr)
        return completed

    def _report(
        self,
        args: str | list[str],
        shell: bool,
        exit_code: int | None,
        timed_out: bool,
        duration: float,
        stdout: str,
        stderr: str,
    ) -> None:
        stdout_text, stdout_cut = clip(stdout, self._capture_bytes)
        stderr_text, stderr_cut = clip(stderr, self._capture_bytes)
        self._emit(
            {
                "type": "command",
                "argv": args,
                "shell": shell,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration": round(duration, 3),
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_truncated": stdout_cut,
                "stderr_truncated": stderr_cut,
            }
        )

    def ledger(self, name: str, expire: str | float | None = None) -> Ledger:
        """Open the persistent ledger `name` (letters, digits, '_', '.', '-').

        `expire` is a duration ("90d"); entries older than that are forgotten.
        Read-only inside check().
        """
        if not _LEDGER_NAME.fullmatch(name):
            raise ValueError(f"invalid ledger name {name!r}: use letters, digits, '_', '.', '-'")
        seconds = None if expire is None else parse_duration(expire)
        return Ledger(self._data_dir / "ledgers" / f"{name}.json", expire=seconds, writable=self.hook != "check")

    def file_key(self, path: str | os.PathLike[str]) -> str:
        """"<absolute path>|<size>|<mtime_ns>": a new file reusing a name gets a new key."""
        resolved = Path(path).expanduser().resolve()
        info = resolved.stat()
        return f"{resolved}|{info.st_size}|{info.st_mtime_ns}"

    def glob(self, pattern: str) -> list[Path]:
        """Sorted glob matches; expands ~. Relative patterns are relative to watch_dir."""
        return sorted(Path(match) for match in _glob.glob(os.path.expanduser(pattern)))

    def unchanged_for(self, path: str | os.PathLike[str], duration: str | float) -> bool:
        """True when nothing has written to the file for `duration`; False if it does not exist."""
        try:
            mtime = Path(path).expanduser().stat().st_mtime
        except FileNotFoundError:
            return False
        return time.time() - mtime >= parse_duration(duration)
```

`src/keepwatch/__init__.py` (replace):

```python
"""keepwatch: poll conditions and run actions, from login onward."""

__version__ = "0.1.0"

from keepwatch.ctx import CommandFailed, Ctx, Ledger, LedgerReadOnly, Unknown  # noqa: E402

__all__ = ["CommandFailed", "Ctx", "Ledger", "LedgerReadOnly", "Unknown", "__version__"]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_protocol.py tests/test_ctx.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/protocol.py src/keepwatch/ctx.py src/keepwatch/__init__.py tests/test_protocol.py tests/test_ctx.py
git commit -m "Add the plugin API: Ctx, Ledger and protocol helpers"
```

---

### Task 7: The worker process

**Files:**
- Create: `src/keepwatch/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `Ctx`, `Unknown` (Task 6); `encode`, `normalize_payload`, `PROTOCOL_VERSION` (Task 6); `HOOK_NAMES` (Task 3).
- Produces:
  - Command line: `python -m keepwatch.worker --request-fd N --result-fd M`
  - Request (JSON, read to EOF from the request fd): `watch, hook, watch_dir, data_dir, run_dir, poll_id, condition, payload, settings, deadline, capture_bytes, mode` (`"call"` or `"describe"`)
  - Messages written to the result fd, in order: `{"type": "hello", "version", "protocol"}`, any number of `{"type": "log", "level", "logger", "message", "fields", "traceback"?}` and `{"type": "command", ...}`, then exactly one `{"type": "result", "status", ...}` where `status` is `"answer"` (+`answer`, `payload`), `"unknown"` (+`reason`, `payload`), `"ok"` (+`hooks` in describe mode) or `"error"` (+`reason`, `exception` `{type, message, traceback}`)
  - `interpret_check_return(value: Any) -> dict`; `run_request(request: dict, send: Callable[[dict], None]) -> dict`; `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing tests** — `tests/test_worker.py`

```python
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from keepwatch import __version__
from keepwatch.protocol import PROTOCOL_VERSION, decode_lines
from keepwatch.worker import interpret_check_return


def watch(tmp_path, source):
    watch_dir = tmp_path / "w"
    watch_dir.mkdir()
    (watch_dir / "watch.py").write_text(textwrap.dedent(source))
    return watch_dir


def request_for(watch_dir, hook="check", **extra):
    request = {
        "watch": watch_dir.name,
        "hook": hook,
        "watch_dir": str(watch_dir),
        "data_dir": str(watch_dir.parent / "data"),
        "run_dir": str(watch_dir.parent / "run"),
        "poll_id": "p1",
        "condition": False,
        "payload": None,
        "settings": {},
        "deadline": time.time() + 30,
        "capture_bytes": 65536,
        "mode": "call",
    }
    request.update(extra)
    return request


def run_worker(request):
    req_r, req_w = os.pipe()
    res_r, res_w = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-m", "keepwatch.worker", "--request-fd", str(req_r), "--result-fd", str(res_w)],
        pass_fds=(req_r, res_w),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(req_r)
    os.close(res_w)
    with os.fdopen(req_w, "wb") as handle:
        handle.write(json.dumps(request).encode())
    with os.fdopen(res_r, "rb") as handle:
        data = handle.read()
    out, err = proc.communicate(timeout=30)
    messages, bad = decode_lines(data)
    assert bad == []
    return messages, out.decode(), err.decode()


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, {"status": "answer", "answer": True, "payload": None}),
        (False, {"status": "answer", "answer": False, "payload": None}),
        (None, {"status": "unknown", "reason": None, "payload": None}),
        ((True, [Path("/a")]), {"status": "answer", "answer": True, "payload": ["/a"]}),
        ((None, {"n": 1}), {"status": "unknown", "reason": None, "payload": {"n": 1}}),
    ],
)
def test_interpret_check_return(value, expected):
    assert interpret_check_return(value) == expected


def test_interpret_check_return_rejects_other_values():
    result = interpret_check_return(["a.tar.gz"])
    assert result["status"] == "error"
    assert "check returned list" in result["reason"]
    assert interpret_check_return((True, {1, 2}))["reason"].startswith("payload is not JSON-serializable")


def test_check_with_payload_and_logs(tmp_path):
    watch_dir = watch(tmp_path, '''
        def check(ctx):
            ctx.log.info("found %d", 2, extra={"files": ["a", "b"]})
            print("to stdout")
            return True, ["a", "b"]
    ''')
    messages, out, _ = run_worker(request_for(watch_dir))
    assert messages[0] == {"type": "hello", "version": __version__, "protocol": PROTOCOL_VERSION}
    log = messages[1]
    assert (log["type"], log["level"], log["message"], log["fields"]) == ("log", "INFO", "found 2", {"files": ["a", "b"]})
    assert messages[-1] == {"type": "result", "status": "answer", "answer": True, "payload": ["a", "b"]}
    assert out == "to stdout\n"


def test_unknown_with_reason(tmp_path):
    watch_dir = watch(tmp_path, '''
        import keepwatch
        def check(ctx):
            raise keepwatch.Unknown("VPN is down")
    ''')
    messages, _, _ = run_worker(request_for(watch_dir))
    assert messages[-1] == {"type": "result", "status": "unknown", "reason": "VPN is down", "payload": None}


def test_exception_in_action_is_an_error_with_traceback(tmp_path):
    watch_dir = watch(tmp_path, '''
        def on_true(ctx):
            1 / 0
    ''')
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_true", payload=["x"]))
    result = messages[-1]
    assert result["status"] == "error"
    assert result["reason"] == "ZeroDivisionError: division by zero"
    assert "ZeroDivisionError" in result["exception"]["traceback"]


def test_action_success_and_cwd(tmp_path):
    watch_dir = watch(tmp_path, '''
        import os
        def on_true(ctx):
            (ctx.run_dir / "seen").write_text(f"{os.getcwd()}|{ctx.payload}|{ctx.condition}")
    ''')
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_true", payload=["x"], condition=True))
    assert messages[-1] == {"type": "result", "status": "ok"}
    assert (tmp_path / "run" / "seen").read_text() == f"{watch_dir}|['x']|True"


def test_import_failure_is_an_error(tmp_path):
    watch_dir = watch(tmp_path, "import does_not_exist_xyz\n")
    messages, _, _ = run_worker(request_for(watch_dir))
    assert messages[-1]["status"] == "error"
    assert messages[-1]["reason"].startswith("importing watch.py failed: ModuleNotFoundError")


def test_missing_hook_function(tmp_path):
    watch_dir = watch(tmp_path, "def check(ctx):\n    return True\n")
    messages, _, _ = run_worker(request_for(watch_dir, hook="on_fall"))
    assert messages[-1] == {"type": "result", "status": "error", "reason": "watch.py has no function on_fall()"}


def test_describe_lists_hooks(tmp_path):
    watch_dir = watch(tmp_path, "def check(ctx):\n    pass\n\ndef on_true(ctx):\n    pass\n\ndef helper():\n    pass\n")
    messages, _, _ = run_worker(request_for(watch_dir, mode="describe"))
    assert messages[-1] == {"type": "result", "status": "ok", "hooks": ["check", "on_true"]}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_worker.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.worker'`

- [ ] **Step 3: Implement** — `src/keepwatch/worker.py`

```python
"""Child side of one Python hook call: `python -m keepwatch.worker`. Stdlib only.

Reads one request (JSON) from --request-fd, imports the watch's watch.py, calls
one function with a Ctx, and writes JSON-line messages to --result-fd: hello,
then log/command messages, then exactly one result.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keepwatch import __version__
from keepwatch.ctx import Ctx, Unknown
from keepwatch.hooks import CHECK, HOOK_NAMES, WATCH_PY
from keepwatch.protocol import PROTOCOL_VERSION, encode, normalize_payload

Send = Callable[[dict[str, Any]], None]
_STANDARD_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class _PipeHandler(logging.Handler):
    """Forwards plugin log records to the service as `log` messages."""

    def __init__(self, send: Send) -> None:
        super().__init__(logging.DEBUG)
        self._send = send

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = {key: value for key, value in vars(record).items() if key not in _STANDARD_RECORD_KEYS}
            message: dict[str, Any] = {
                "type": "log",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "fields": fields,
            }
            if record.exc_info:
                message["traceback"] = "".join(traceback.format_exception(*record.exc_info))
            self._send(message)
        except Exception:
            self.handleError(record)


def _exception_info(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(exc)),
    }


def _load_module(watch_dir: Path) -> Any:
    spec = importlib.util.spec_from_file_location("keepwatch_watch", watch_dir / WATCH_PY)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {watch_dir / WATCH_PY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["keepwatch_watch"] = module
    spec.loader.exec_module(module)
    return module


def interpret_check_return(value: Any) -> dict[str, Any]:
    """Turn check()'s return value into a result (see `keepwatch docs python`)."""
    if value is True or value is False:
        return {"status": "answer", "answer": value, "payload": None}
    if value is None:
        return {"status": "unknown", "reason": None, "payload": None}
    if isinstance(value, tuple) and len(value) == 2 and (value[0] is None or isinstance(value[0], bool)):
        answer, raw_payload = value
        payload, problem = normalize_payload(raw_payload)
        if problem is not None:
            return {"status": "error", "reason": problem}
        if answer is None:
            return {"status": "unknown", "reason": None, "payload": payload}
        return {"status": "answer", "answer": answer, "payload": payload}
    shown = repr(value)[:80]
    return {
        "status": "error",
        "reason": f"check returned {type(value).__name__} {shown}; "
        "expected True, False, None, or a tuple (answer, payload)",
    }


def run_request(request: dict[str, Any], send: Send) -> dict[str, Any]:
    """Perform one request and return the result fields (without "type")."""
    watch_dir = Path(request["watch_dir"])
    hook = request["hook"]
    os.chdir(watch_dir)
    sys.path.insert(0, str(watch_dir))
    logger = logging.getLogger(f"keepwatch.watch.{request['watch']}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_PipeHandler(send))
    try:
        module = _load_module(watch_dir)
    except BaseException as exc:
        return {
            "status": "error",
            "reason": f"importing watch.py failed: {type(exc).__name__}: {exc}",
            "exception": _exception_info(exc),
        }
    if request.get("mode") == "describe":
        return {"status": "ok", "hooks": sorted(name for name in HOOK_NAMES if callable(getattr(module, name, None)))}
    function = getattr(module, hook, None)
    if not callable(function):
        return {"status": "error", "reason": f"watch.py has no function {hook}()"}
    ctx = Ctx(
        watch=request["watch"],
        hook=hook,
        poll_id=request["poll_id"],
        condition=request["condition"],
        payload=request.get("payload"),
        settings=request.get("settings") or {},
        watch_dir=watch_dir,
        data_dir=Path(request["data_dir"]),
        run_dir=Path(request["run_dir"]),
        deadline=request["deadline"],
        capture_bytes=request.get("capture_bytes", 65_536),
        emit=send,
    )
    try:
        value = function(ctx)
    except Unknown as exc:
        if hook == CHECK:
            return {"status": "unknown", "reason": exc.reason or None, "payload": None}
        return {
            "status": "error",
            "reason": "keepwatch.Unknown only has a meaning in check()",
            "exception": _exception_info(exc),
        }
    except BaseException as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "exception": _exception_info(exc)}
    if hook == CHECK:
        return interpret_check_return(value)
    return {"status": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m keepwatch.worker")
    parser.add_argument("--request-fd", type=int, required=True)
    parser.add_argument("--result-fd", type=int, required=True)
    args = parser.parse_args(argv)
    with os.fdopen(args.request_fd, "rb") as handle:
        request = json.loads(handle.read())
    out = os.fdopen(args.result_fd, "wb", buffering=0)

    def send(message: dict[str, Any]) -> None:
        out.write(encode(message))

    send({"type": "hello", "version": __version__, "protocol": PROTOCOL_VERSION})
    result = run_request(request, send)
    send({"type": "result", **result})
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_worker.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/worker.py tests/test_worker.py
git commit -m "Add the worker process for Python hooks"
```

---

### Task 8: Runner for Python hooks

**Files:**
- Create: `src/keepwatch/runner.py`
- Modify: `tests/conftest.py` (add the `call_for` fixture)
- Test: `tests/test_runner_python.py`

**Interfaces:**
- Consumes: `WatchConfig`, `Command`, `ExitCodes` (Task 3); `Outcome` (Task 5); `decode_lines`, `PROTOCOL_VERSION`, `normalize_payload` (Task 6); `format_duration` (Task 1); the worker command line (Task 7).
- Produces:
  - `HookCall(watch: WatchConfig, hook: str, poll_id: str, condition: bool, payload: Any, data_dir: Path, run_dir: Path, timeout: float, capture_bytes: int = 65_536, environment: Mapping[str, str] = {}, mode: str = "call")` (frozen). `environment` is the global `[environment]`; the runner adds the watch's own.
  - `HookResult` (dataclass) fields: `hook, kind ("python"|"command"), target, status, payload=None, reason=None, exit_code=None, signal=None, exception=None, stdout="", stderr="", stdout_truncated=False, stderr_truncated=False, duration=0.0, messages=[], hooks=None`; `status` is an `Outcome` value for checks, `"ok"`/`"failed"`/`"timeout"` for actions, `"ok"`/`"error"`/`"timeout"` for describe; property `succeeded` (`status == "ok"`); `outcome() -> Outcome`; `to_record() -> dict` (all fields except `messages` and `hooks`)
  - `Runner(*, python: str = sys.executable, kill_grace: float = 5.0)` with `run(call: HookCall) -> HookResult`
  - Test fixture `call_for(watch_dir, hook="check", *, timeout=30.0, condition=False, payload=None, capture_bytes=65536, environment=None, mode="call") -> HookCall`

- [ ] **Step 1: Add the fixture** — append to `tests/conftest.py` (and add `import os` at the top)

```python
@pytest.fixture
def call_for(xdg):
    from keepwatch.config import load_watch_config
    from keepwatch.runner import HookCall

    def make(watch_dir, hook="check", *, timeout=30.0, condition=False, payload=None,
             capture_bytes=65536, environment=None, mode="call"):
        watch = load_watch_config(watch_dir)
        return HookCall(
            watch=watch,
            hook=hook,
            poll_id="p1",
            condition=condition,
            payload=payload,
            data_dir=xdg.watch_data_dir(watch.name),
            run_dir=xdg.run_dir(os.getpid(), watch.name),
            timeout=timeout,
            capture_bytes=capture_bytes,
            environment=environment or {},
            mode=mode,
        )

    return make
```

- [ ] **Step 2: Write the failing tests** — `tests/test_runner_python.py`

```python
import os
import time
from pathlib import Path

from keepwatch import runner as runner_module
from keepwatch.runner import Runner
from keepwatch.state import Outcome


def gone(pid, within=5.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


def test_check_answers_with_payload_logs_and_output(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            ctx.log.warning("two files")
            print("hello from check")
            return True, ["a", "b"]
    '''})
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.kind, result.target) == ("true", "python", "watch.py:check")
    assert result.outcome() is Outcome.TRUE
    assert result.payload == ["a", "b"]
    assert result.stdout == "hello from check\n"
    assert [m["message"] for m in result.messages if m["type"] == "log"] == ["two files"]
    record = result.to_record()
    assert "messages" not in record and record["status"] == "true"


def test_false_and_unknown(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        import os
        def check(ctx):
            return None if os.environ.get("ANSWER") == "none" else False
    '''})
    assert Runner().run(call_for(watch_dir)).status == "false"
    assert Runner().run(call_for(watch_dir, environment={"ANSWER": "none"})).status == "unknown"


def test_check_exception_is_error_and_action_exception_is_failed(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": '''
        def check(ctx):
            1 / 0
        def on_true(ctx):
            raise RuntimeError("scp broke")
    '''})
    check = Runner().run(call_for(watch_dir))
    assert check.status == "error" and "ZeroDivisionError" in check.exception["traceback"]
    action = Runner().run(call_for(watch_dir, hook="on_true"))
    assert action.status == "failed" and action.reason == "RuntimeError: scp broke"
    assert action.succeeded is False


def test_action_success(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "ok" and result.succeeded


def test_import_error_is_reported(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "import does_not_exist_xyz\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("importing watch.py failed: ModuleNotFoundError")


def test_timeout_kills_the_whole_process_group(xdg, make_watch, call_for):
    watch_dir = make_watch("slow", files={"watch.py": '''
        import subprocess, time
        def check(ctx):
            child = subprocess.Popen(["sleep", "60"])
            (ctx.run_dir / "child.pid").write_text(str(child.pid))
            time.sleep(60)
    '''})
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.5))
    assert result.status == "timeout"
    assert result.reason == "timed out after 1.5s"
    assert result.duration < 10
    child_pid = int((xdg.run_dir(os.getpid(), "slow") / "child.pid").read_text())
    assert gone(child_pid)


def test_python_dependencies_not_supported_yet(make_watch, call_for):
    watch_dir = make_watch("w", config='python_dependencies = ["requests"]\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert "python_dependencies is not supported" in result.reason


def test_version_mismatch_is_reported(make_watch, call_for, monkeypatch):
    monkeypatch.setattr(runner_module, "_EXPECTED_VERSION", "0.0.0-test")
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    return True\n"})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("keepwatch version mismatch")


def test_describe_mode(make_watch, call_for):
    watch_dir = make_watch("w", files={"watch.py": "def check(ctx):\n    pass\n\ndef on_true(ctx):\n    pass\n"})
    result = Runner().run(call_for(watch_dir, mode="describe"))
    assert result.status == "ok" and result.hooks == ["check", "on_true"]


def test_keepwatch_environment_reaches_python_hooks(make_watch, call_for):
    watch_dir = make_watch("w", config='[environment]\nFROM_WATCH = "1"\n', files={"watch.py": '''
        import os
        def check(ctx):
            return True, {k: os.environ.get(k) for k in ("KEEPWATCH_WATCH", "KEEPWATCH_HOOK", "FROM_WATCH")}
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.payload == {"KEEPWATCH_WATCH": "w", "KEEPWATCH_HOOK": "check", "FROM_WATCH": "1"}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_runner_python.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.runner'`

- [ ] **Step 4: Implement** — `src/keepwatch/runner.py`

```python
"""Run one hook call in a child process: a Python worker or a command.

Every call gets its own session (process group). At the deadline the group
gets SIGTERM, then SIGKILL after kill_grace seconds. stdout and stderr are
captured (head and tail kept); everything is returned as a HookResult.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any

from keepwatch import __version__
from keepwatch.config import Command, ExitCodes, WatchConfig
from keepwatch.durations import format_duration
from keepwatch.hooks import CHECK
from keepwatch.protocol import PROTOCOL_VERSION, decode_lines, normalize_payload
from keepwatch.state import Outcome

KILL_GRACE = 5.0
DRAIN_GRACE = 2.0
_EXPECTED_VERSION = __version__


@dataclass(frozen=True)
class HookCall:
    watch: WatchConfig
    hook: str
    poll_id: str
    condition: bool
    payload: Any
    data_dir: Path
    run_dir: Path
    timeout: float
    capture_bytes: int = 65_536
    environment: Mapping[str, str] = field(default_factory=dict)
    mode: str = "call"


@dataclass
class HookResult:
    hook: str
    kind: str
    target: str
    status: str
    payload: Any = None
    reason: str | None = None
    exit_code: int | None = None
    signal: int | None = None
    exception: dict[str, str] | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[str] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"

    def outcome(self) -> Outcome:
        return Outcome(self.status)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        del record["messages"]
        del record["hooks"]
        return record


class _Reader(threading.Thread):
    """Drains one pipe. With a limit, keeps only the first and last limit/2 bytes."""

    def __init__(self, stream: IO[bytes], limit: int | None) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self) -> None:
        fd = self._stream.fileno()
        try:
            while chunk := os.read(fd, 65536):
                self.total += len(chunk)
                if self._limit is None:
                    self.head += chunk
                    continue
                half = max(self._limit // 2, 1)
                room = half - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > half:
                        del self.tail[:-half]
        finally:
            self._stream.close()

    def raw(self) -> bytes:
        return bytes(self.head) + bytes(self.tail)

    def text(self) -> tuple[str, bool]:
        omitted = self.total - len(self.head) - len(self.tail)
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail.decode("utf-8", errors="replace")
        if omitted <= 0:
            return (bytes(self.head) + bytes(self.tail)).decode("utf-8", errors="replace"), False
        return f"{head}\n…[{omitted} bytes omitted]…\n{tail}", True


class _Writer(threading.Thread):
    def __init__(self, fd: int, data: bytes) -> None:
        super().__init__(daemon=True)
        self._fd = fd
        self._data = data

    def run(self) -> None:
        try:
            with os.fdopen(self._fd, "wb") as handle:
                handle.write(self._data)
        except BrokenPipeError:
            pass


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


def classify_exit(code: int, codes: ExitCodes) -> str:
    """Map a command check's exit code to an outcome value via [check_exit_codes]."""
    if code in codes.true:
        return "true"
    if code in codes.false:
        return "false"
    if code in codes.unknown:
        return "unknown"
    return "error"


def setting_variables(settings: Mapping[str, Any]) -> dict[str, str]:
    """KEEPWATCH_SETTING_<KEY> for each top-level scalar setting."""
    variables = {}
    for key, value in settings.items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            text = str(value)
        else:
            continue
        variables[f"KEEPWATCH_SETTING_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}"] = text
    return variables


class Runner:
    def __init__(self, *, python: str = sys.executable, kill_grace: float = KILL_GRACE) -> None:
        self.python = python
        self.kill_grace = kill_grace

    def run(self, call: HookCall) -> HookResult:
        if call.mode == "call" and call.hook in call.watch.hooks:
            return self._run_command(call, call.watch.hooks[call.hook])
        return self._run_python(call)

    # -- shared -------------------------------------------------------------

    def _failure_status(self, call: HookCall) -> str:
        return "error" if call.hook == CHECK or call.mode == "describe" else "failed"

    def _error(self, call: HookCall, kind: str, target: str, started: float, reason: str) -> HookResult:
        return HookResult(
            hook=call.hook,
            kind=kind,
            target=target,
            status=self._failure_status(call),
            reason=reason,
            duration=time.monotonic() - started,
        )

    def _environment(self, call: HookCall) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("KEEPWATCH_") or key == "KEEPWATCH_CONFIG"
        }
        env.update(call.environment)
        env.update(call.watch.environment)
        env.update(setting_variables(call.watch.settings))
        env.update(
            {
                "KEEPWATCH_WATCH": call.watch.name,
                "KEEPWATCH_HOOK": call.hook,
                "KEEPWATCH_POLL_ID": call.poll_id,
                "KEEPWATCH_CONDITION": "true" if call.condition else "false",
                "KEEPWATCH_WATCH_DIR": str(call.watch.watch_dir),
                "KEEPWATCH_DATA_DIR": str(call.data_dir),
                "KEEPWATCH_RUN_DIR": str(call.run_dir),
            }
        )
        return env

    def _supervise(self, proc: subprocess.Popen[bytes], readers: Sequence[_Reader], timeout: float) -> tuple[int, bool]:
        timed_out = False
        try:
            proc.wait(timeout=max(timeout, 0.0))
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=self.kill_grace)
            except subprocess.TimeoutExpired:
                pass
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait()
        drain_until = time.monotonic() + DRAIN_GRACE
        for reader in readers:
            reader.join(max(drain_until - time.monotonic(), 0.0))
        if any(reader.is_alive() for reader in readers):
            # A leftover child still holds a pipe open: kill the whole group.
            _signal_group(proc.pid, signal.SIGKILL)
            for reader in readers:
                reader.join(DRAIN_GRACE)
        return proc.returncode, timed_out

    def _captured(self, out: _Reader, err: _Reader, returncode: int, started: float) -> dict[str, Any]:
        stdout, stdout_cut = out.text()
        stderr, stderr_cut = err.text()
        return {
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_cut,
            "stderr_truncated": stderr_cut,
            "exit_code": returncode if returncode >= 0 else None,
            "signal": -returncode if returncode < 0 else None,
            "duration": time.monotonic() - started,
        }

    # -- Python hooks ---------------------------------------------------------

    def _run_python(self, call: HookCall) -> HookResult:
        target = f"watch.py:{call.hook}" if call.mode == "call" else "watch.py (describe)"
        started = time.monotonic()
        if call.watch.python_dependencies:
            return self._error(
                call,
                "python",
                target,
                started,
                "python_dependencies is not supported by this version of keepwatch; remove it from config.toml",
            )
        request = {
            "watch": call.watch.name,
            "hook": call.hook,
            "watch_dir": str(call.watch.watch_dir),
            "data_dir": str(call.data_dir),
            "run_dir": str(call.run_dir),
            "poll_id": call.poll_id,
            "condition": call.condition,
            "payload": call.payload,
            "settings": dict(call.watch.settings),
            "deadline": time.time() + call.timeout,
            "capture_bytes": call.capture_bytes,
            "mode": call.mode,
        }
        env = self._environment(call)
        env["PYTHONUNBUFFERED"] = "1"
        req_r, req_w = os.pipe()
        res_r, res_w = os.pipe()
        argv = [self.python, "-m", "keepwatch.worker", "--request-fd", str(req_r), "--result-fd", str(res_w)]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=call.watch.watch_dir,
                env=env,
                pass_fds=(req_r, res_w),
                start_new_session=True,
            )
        except OSError as exc:
            for fd in (req_r, req_w, res_r, res_w):
                os.close(fd)
            return self._error(call, "python", target, started, f"cannot start the Python worker ({self.python}): {exc}")
        os.close(req_r)
        os.close(res_w)
        _Writer(req_w, json.dumps(request).encode("utf-8")).start()
        out = _Reader(proc.stdout, call.capture_bytes)
        err = _Reader(proc.stderr, call.capture_bytes)
        results = _Reader(os.fdopen(res_r, "rb"), None)
        for reader in (out, err, results):
            reader.start()
        returncode, timed_out = self._supervise(proc, (out, err, results), call.timeout)
        messages, bad = decode_lines(results.raw())
        base = self._captured(out, err, returncode, started)
        base["messages"] = [message for message in messages if message.get("type") in ("log", "command")]
        if bad:
            base["messages"].append(
                {
                    "type": "log",
                    "level": "WARNING",
                    "logger": "keepwatch.worker",
                    "message": f"ignored {len(bad)} malformed message(s) from the worker",
                    "fields": {},
                }
            )
        common = {"hook": call.hook, "kind": "python", "target": target, **base}
        failure = self._failure_status(call)
        if timed_out:
            return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
        hello = next((m for m in messages if m.get("type") == "hello"), None)
        result = next((m for m in messages if m.get("type") == "result"), None)
        if hello is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) before starting; see stderr",
                **common,
            )
        if hello.get("protocol") != PROTOCOL_VERSION or hello.get("version") != _EXPECTED_VERSION:
            return HookResult(
                status=failure,
                reason=(
                    f"keepwatch version mismatch: service {_EXPECTED_VERSION} (protocol {PROTOCOL_VERSION}), "
                    f"worker {hello.get('version')} (protocol {hello.get('protocol')})"
                ),
                **common,
            )
        if result is None:
            return HookResult(
                status=failure,
                reason=f"the Python worker exited (code {returncode}) without reporting a result; see stderr",
                **common,
            )
        status = result.get("status")
        details = {"reason": result.get("reason"), "exception": result.get("exception")}
        if call.mode == "describe":
            return HookResult(status="ok" if status == "ok" else "error", hooks=result.get("hooks"), **details, **common)
        if call.hook == CHECK:
            if status == "answer":
                mapped = "true" if result.get("answer") else "false"
            elif status == "unknown":
                mapped = "unknown"
            else:
                mapped = "error"
            return HookResult(status=mapped, payload=result.get("payload"), **details, **common)
        return HookResult(status="ok" if status == "ok" else "failed", **details, **common)

    # -- command hooks (Task 9) -----------------------------------------------

    def _run_command(self, call: HookCall, command: Command) -> HookResult:
        started = time.monotonic()
        return self._error(call, "command", command.display(), started, "command hooks are added in Task 9")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_runner_python.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/runner.py tests/conftest.py tests/test_runner_python.py
git commit -m "Run Python hooks in isolated worker processes with real timeouts"
```

---

### Task 9: Runner for command hooks

**Files:**
- Modify: `src/keepwatch/runner.py` (replace the `_run_command` stub; add `_read_payload`)
- Test: `tests/test_runner_command.py`

**Interfaces:**
- Consumes: Task 8's `Runner`, `HookCall`, `HookResult`, `classify_exit`, `setting_variables`.
- Produces: command hooks per spec 9.3 — cwd is the watch dir, stdin `/dev/null`, env adds `KEEPWATCH_SETTINGS_FILE`, `KEEPWATCH_PAYLOAD_OUT` (check), `KEEPWATCH_PAYLOAD_FILE` (actions with a payload); temporary files in `run_dir` are removed after the call.

- [ ] **Step 1: Write the failing tests** — `tests/test_runner_command.py`

```python
import json
import os

from keepwatch.runner import Runner

ACT_SH = '''
    #!/bin/sh
    {
      echo "condition=$KEEPWATCH_CONDITION"
      echo "hook=$KEEPWATCH_HOOK"
      echo "dest=$KEEPWATCH_SETTING_DEST"
      echo "extra=$EXTRA"
      echo "global=$GLOBAL_ONLY"
      echo "payload=$(cat "$KEEPWATCH_PAYLOAD_FILE")"
      echo "settings=$(cat "$KEEPWATCH_SETTINGS_FILE")"
      echo "cwd=$(pwd)"
    } > "$KEEPWATCH_RUN_DIR/seen.txt"
'''


def test_exit_codes_map_to_outcomes(make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        check = "exit $CODE"
        [check_exit_codes]
        unknown = [3]
    ''')
    for code, status in ((0, "true"), (1, "false"), (3, "unknown"), (7, "error")):
        result = Runner().run(call_for(watch_dir, environment={"CODE": str(code)}))
        assert (result.status, result.exit_code, result.kind) == (status, code, "command")
    assert "exit code 7 is not listed in [check_exit_codes]" in result.reason


def test_check_passes_a_payload(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.sh"]\n', files={"check.sh": '''
        #!/bin/sh
        printf '["a.tar.gz", "b.tar.gz"]' > "$KEEPWATCH_PAYLOAD_OUT"
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.payload == ["a.tar.gz", "b.tar.gz"]
    assert result.target == "./check.sh"


def test_invalid_payload_json_is_an_error(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./check.sh"]\n', files={"check.sh": '''
        #!/bin/sh
        echo "not json" > "$KEEPWATCH_PAYLOAD_OUT"
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "error"
    assert result.reason.startswith("$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON")


def test_action_sees_environment_payload_and_settings(xdg, make_watch, call_for):
    watch_dir = make_watch("cmd", config='''
        [hooks]
        on_true = ["./act.sh"]
        [environment]
        EXTRA = "watch-level"
        [settings]
        dest = "foo@bar:/in/"
    ''', files={"act.sh": ACT_SH})
    result = Runner().run(call_for(watch_dir, hook="on_true", condition=True, payload=["a.tar.gz"],
                                   environment={"GLOBAL_ONLY": "yes"}))
    assert result.status == "ok"
    run_dir = xdg.run_dir(os.getpid(), "cmd")
    seen = (run_dir / "seen.txt").read_text().splitlines()
    assert seen == [
        "condition=true",
        "hook=on_true",
        "dest=foo@bar:/in/",
        "extra=watch-level",
        "global=yes",
        'payload=["a.tar.gz"]',
        "settings=" + json.dumps({"dest": "foo@bar:/in/"}),
        f"cwd={watch_dir}",
    ]
    assert sorted(p.name for p in run_dir.iterdir()) == ["seen.txt"]


def test_failed_action(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_false = "echo oops >&2; exit 4"\n')
    result = Runner().run(call_for(watch_dir, hook="on_false"))
    assert (result.status, result.exit_code, result.stderr) == ("failed", 4, "oops\n")
    assert result.reason == "exit code 4"


def test_missing_executable(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\non_true = ["./nope.sh"]\n')
    result = Runner().run(call_for(watch_dir, hook="on_true"))
    assert result.status == "failed"
    assert result.reason.startswith("cannot start command")


def test_killed_by_signal(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "kill -TERM $$"\n')
    result = Runner().run(call_for(watch_dir))
    assert (result.status, result.signal, result.reason) == ("error", 15, "killed by signal SIGTERM")


def test_timeout(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30"\n')
    result = Runner(kill_grace=1.0).run(call_for(watch_dir, timeout=1.0))
    assert result.status == "timeout" and result.duration < 10


def test_background_child_holding_stdout_does_not_hang(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "sleep 30 & echo started"\n')
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "started\n"
    assert result.duration < 10


def test_binary_output_is_decoded_with_replacement(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = ["./bin.sh"]\n', files={"bin.sh": '''
        #!/bin/sh
        printf '\\377ok'
    '''})
    result = Runner().run(call_for(watch_dir))
    assert result.status == "true"
    assert result.stdout == "\ufffdok"


def test_output_is_clipped(make_watch, call_for):
    watch_dir = make_watch("cmd", config='[hooks]\ncheck = "yes x | head -c 10000"\n')
    result = Runner().run(call_for(watch_dir, capture_bytes=1000))
    assert result.stdout_truncated is True
    assert "bytes omitted" in result.stdout
    assert len(result.stdout) < 1200
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_runner_command.py -q`
Expected: FAIL (every test reports `command hooks are added in Task 9`)

- [ ] **Step 3: Implement** — in `src/keepwatch/runner.py`, replace the `_run_command` stub section with:

```python
    # -- command hooks --------------------------------------------------------

    def _run_command(self, call: HookCall, command: Command) -> HookResult:
        started = time.monotonic()
        target = command.display()
        env = self._environment(call)
        temp_files: list[Path] = []
        try:
            call.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            call.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            stem = f"{call.poll_id}-{call.hook}"
            settings_file = call.run_dir / f"{stem}-settings.json"
            settings_file.write_text(json.dumps(dict(call.watch.settings)), encoding="utf-8")
            temp_files.append(settings_file)
            env["KEEPWATCH_SETTINGS_FILE"] = str(settings_file)
            payload_out: Path | None = None
            if call.hook == CHECK:
                payload_out = call.run_dir / f"{stem}-payload-out.json"
                payload_out.unlink(missing_ok=True)
                temp_files.append(payload_out)
                env["KEEPWATCH_PAYLOAD_OUT"] = str(payload_out)
            elif call.payload is not None:
                payload_file = call.run_dir / f"{stem}-payload.json"
                payload_file.write_text(json.dumps(call.payload), encoding="utf-8")
                temp_files.append(payload_file)
                env["KEEPWATCH_PAYLOAD_FILE"] = str(payload_file)
            try:
                proc = subprocess.Popen(
                    command.to_argv(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=call.watch.watch_dir,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                return self._error(call, "command", target, started, f"cannot start command: {exc.strerror or exc}")
            out = _Reader(proc.stdout, call.capture_bytes)
            err = _Reader(proc.stderr, call.capture_bytes)
            out.start()
            err.start()
            returncode, timed_out = self._supervise(proc, (out, err), call.timeout)
            common = {
                "hook": call.hook,
                "kind": "command",
                "target": target,
                **self._captured(out, err, returncode, started),
            }
            if timed_out:
                return HookResult(status="timeout", reason=f"timed out after {format_duration(call.timeout)}", **common)
            if returncode < 0:
                return HookResult(
                    status=self._failure_status(call),
                    reason=f"killed by signal {_signal_name(-returncode)}",
                    **common,
                )
            if call.hook != CHECK:
                if returncode == 0:
                    return HookResult(status="ok", **common)
                return HookResult(status="failed", reason=f"exit code {returncode}", **common)
            status = classify_exit(returncode, call.watch.exit_codes)
            if status == "error":
                codes = call.watch.exit_codes
                return HookResult(
                    status="error",
                    reason=(
                        f"exit code {returncode} is not listed in [check_exit_codes] "
                        f"(true={list(codes.true)}, false={list(codes.false)}, unknown={list(codes.unknown)})"
                    ),
                    **common,
                )
            payload, problem = self._read_payload(payload_out)
            if problem is not None:
                return HookResult(status="error", reason=problem, **common)
            return HookResult(status=status, payload=payload, **common)
        finally:
            for path in temp_files:
                path.unlink(missing_ok=True)

    @staticmethod
    def _read_payload(path: Path | None) -> tuple[Any, str | None]:
        if path is None or not path.exists() or path.stat().st_size == 0:
            return None, None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"$KEEPWATCH_PAYLOAD_OUT does not contain valid JSON: {exc}"
        return normalize_payload(raw)
```

- [ ] **Step 4: Run the runner tests**

Run: `uv run pytest tests/test_runner_command.py tests/test_runner_python.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/runner.py tests/test_runner_command.py
git commit -m "Run command hooks with exit-code mapping, payload files and settings env"
```

---

### Task 10: Log records, the log file and terminal formatting

**Files:**
- Create: `src/keepwatch/logstore.py`
- Create: `src/keepwatch/output.py`
- Test: `tests/test_logstore.py`, `tests/test_output.py`

**Interfaces:**
- Consumes: `hold_lock` (Task 2).
- Produces:
  - `logstore.Sink = Callable[[dict], None]`; `make_record(event: str, *, level: str = "INFO", **fields) -> dict` (adds `ts` RFC 3339 with offset and milliseconds, `level`, `event`, `pid`); `fan_out(*sinks) -> Sink`
  - `LogWriter(path: Path, *, max_bytes: int, backups: int)` with `write(record: dict) -> None` (thread- and process-safe; rotates to `<name>.1 … <name>.<backups>`)
  - `output.plain_output(stream=None) -> bool`; `make_console(*, stderr: bool = False) -> rich.console.Console`; `format_record(record: dict, *, verbose: bool = False) -> str`; `ConsolePrinter(console, *, verbose=False)` callable as a `Sink`

- [ ] **Step 1: Write the failing tests** — `tests/test_logstore.py`

```python
import json
import re
import threading

from keepwatch.logstore import LogWriter, fan_out, make_record


def test_make_record():
    record = make_record("poll.start", watch="w", poll_id="p1")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d", record["ts"])
    assert (record["level"], record["event"], record["watch"]) == ("INFO", "poll.start", "w")
    assert isinstance(record["pid"], int)


def test_fan_out():
    first, second = [], []
    fan_out(first.append, second.append)({"a": 1})
    assert first == second == [{"a": 1}]


def test_writer_appends_json_lines(tmp_path):
    path = tmp_path / "logs" / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=10_000, backups=2)
    writer.write({"event": "a", "path": tmp_path})
    writer.write({"event": "b"})
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["a", "b"]
    assert lines[0]["path"] == str(tmp_path)


def test_writer_rotates(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=200, backups=2)
    for index in range(40):
        writer.write({"event": "x", "n": index, "pad": "y" * 50})
    names = sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith("."))
    assert names == ["keepwatch.jsonl", "keepwatch.jsonl.1", "keepwatch.jsonl.2"]
    last = json.loads(path.read_text().splitlines()[-1])
    assert last["n"] == 39


def test_writer_is_thread_safe(tmp_path):
    path = tmp_path / "keepwatch.jsonl"
    writer = LogWriter(path, max_bytes=10_000_000, backups=1)
    threads = [threading.Thread(target=lambda: [writer.write({"event": "t"}) for _ in range(50)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 200
    assert all(json.loads(line)["event"] == "t" for line in lines)
```

`tests/test_output.py`:

```python
import io

from keepwatch.output import format_record, plain_output

BASE = {"ts": "2026-09-29T10:11:12.345-05:00", "level": "INFO", "watch": "psg", "poll_id": "abc123"}


def test_plain_output_detection(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert plain_output(io.StringIO()) is True
    monkeypatch.setenv("NO_COLOR", "1")

    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert plain_output(Tty()) is True
    monkeypatch.delenv("NO_COLOR")
    assert plain_output(Tty()) is False


def test_format_poll_start():
    line = format_record({**BASE, "event": "poll.start", "condition": False, "faked": True, "dry_run": False})
    assert line == "10:11:12 psg poll abc123 start: condition FALSE [fake]"


def test_format_hook_end_shows_output_on_failure():
    record = {
        **BASE,
        "event": "hook.end",
        "hook": "on_true",
        "status": "failed",
        "duration": 1.234,
        "target": "watch.py:on_true",
        "reason": "CommandFailed: command exited 1: scp a b",
        "exit_code": None,
        "signal": None,
        "stdout": "",
        "stderr": "Permission denied\n",
        "exception": None,
    }
    text = format_record(record)
    assert text.startswith("10:11:12 psg on_true → failed (1.23s) watch.py:on_true: CommandFailed")
    assert "  stderr:\n    Permission denied" in text


def test_format_check_outcome():
    record = {
        **BASE,
        "event": "check.outcome",
        "outcome": "true",
        "reason": None,
        "payload": ["a"],
        "condition_before": False,
        "condition_after": True,
        "actions": ["on_rise", "on_true"],
    }
    assert format_record(record) == "10:11:12 psg outcome true; condition FALSE → TRUE; actions: on_rise, on_true"
    assert '  payload: ["a"]' in format_record(record, verbose=True)


def test_format_poll_end_and_unknown_event():
    end = {**BASE, "event": "poll.end", "failed": True, "failures": 2, "dry_run": False}
    assert format_record(end) == "10:11:12 psg poll FAILED; failures 2"
    other = format_record({**BASE, "event": "custom.thing", "x": 1})
    assert other.startswith("10:11:12 psg custom.thing ")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_logstore.py tests/test_output.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.logstore'`

- [ ] **Step 3: Implement** — `src/keepwatch/logstore.py`

```python
"""Log records and the JSON-lines log file (spec section 12.1)."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from keepwatch.locks import hold_lock

Sink = Callable[[dict[str, Any]], None]


def make_record(event: str, *, level: str = "INFO", **fields: Any) -> dict[str, Any]:
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        "pid": os.getpid(),
        **fields,
    }


def fan_out(*sinks: Sink) -> Sink:
    def emit(record: dict[str, Any]) -> None:
        for sink in sinks:
            sink(record)

    return emit


class LogWriter:
    """Appends records as JSON lines; rotates by size. Safe across threads and processes."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._lock_path = path.parent / f".{path.name}.lock"

    def write(self, record: dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with hold_lock(self._lock_path):
                try:
                    size = self.path.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size and size + len(line) > self.max_bytes:
                    self._rotate()
                with open(self.path, "ab") as handle:
                    handle.write(line)

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        self._backup(self.backups).unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = self._backup(index)
            if source.exists():
                source.rename(self._backup(index + 1))
        self.path.rename(self._backup(1))
```

`src/keepwatch/output.py`:

```python
"""Terminal output: plain for agents and pipes, Rich on a terminal."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

LEVEL_STYLES = {"DEBUG": "dim", "INFO": "", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold red"}
_FAILED_STATUSES = ("error", "failed", "timeout")


def plain_output(stream: TextIO | None = None) -> bool:
    """True when output should be plain: not a terminal, or NO_COLOR is set."""
    stream = sys.stdout if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return not (isatty is not None and isatty())


def make_console(*, stderr: bool = False) -> Console:
    """A Console for the current stdout/stderr (call it inside a command, not at import)."""
    stream = sys.stderr if stderr else sys.stdout
    if plain_output(stream):
        return Console(
            file=stream,
            color_system=None,
            force_terminal=False,
            highlight=False,
            markup=False,
            emoji=False,
            soft_wrap=True,
        )
    return Console(file=stream, highlight=False, markup=False, emoji=False)


def _condition(value: Any) -> str:
    return "TRUE" if value else "FALSE"


def _block(name: str, text: str) -> str:
    return f"\n  {name}:\n" + textwrap.indent(text.rstrip("\n"), "    ")


def _poll_start(record: dict[str, Any], verbose: bool) -> str:
    tags = [tag for tag, on in (("fake", record.get("faked")), ("dry run", record.get("dry_run"))) if on]
    suffix = f" [{', '.join(tags)}]" if tags else ""
    return f"poll {record.get('poll_id')} start: condition {_condition(record.get('condition'))}{suffix}"


def _hook_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"{record.get('hook')} → {record.get('status')} ({record.get('duration', 0):.2f}s) {record.get('target')}"
    if record.get("reason"):
        line += f": {record['reason']}"
    if record.get("exit_code") not in (None, 0):
        line += f" [exit {record['exit_code']}]"
    if record.get("signal"):
        line += f" [signal {record['signal']}]"
    if verbose or record.get("status") in _FAILED_STATUSES:
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
        exception = record.get("exception") or {}
        if exception.get("traceback"):
            line += _block("traceback", exception["traceback"])
    return line


def _check_outcome(record: dict[str, Any], verbose: bool) -> str:
    before, after = record.get("condition_before"), record.get("condition_after")
    change = _condition(before) if before == after else f"{_condition(before)} → {_condition(after)}"
    actions = ", ".join(record.get("actions") or []) or "none"
    line = f"outcome {record.get('outcome')}; condition {change}; actions: {actions}"
    if record.get("reason"):
        line += f"; reason: {record['reason']}"
    if verbose and record.get("payload") is not None:
        line += f"\n  payload: {json.dumps(record['payload'], ensure_ascii=False)[:2000]}"
    return line


def _command(record: dict[str, Any], verbose: bool) -> str:
    argv = record.get("argv")
    shown = argv if isinstance(argv, str) else " ".join(str(item) for item in argv or [])
    result = "timed out" if record.get("timed_out") else f"exit {record.get('exit_code')}"
    line = f"  $ {shown} → {result} ({record.get('duration', 0):.2f}s)"
    if verbose or record.get("exit_code") not in (0,):
        for name in ("stdout", "stderr"):
            text = record.get(name) or ""
            if text.strip():
                line += _block(name, text)
    return line


def _plugin_log(record: dict[str, Any], verbose: bool) -> str:
    line = f"  [{record.get('level')}] {record.get('message')}"
    if record.get("fields"):
        line += f" {json.dumps(record['fields'], ensure_ascii=False, default=str)}"
    if record.get("traceback"):
        line += _block("traceback", record["traceback"])
    return line


def _poll_end(record: dict[str, Any], verbose: bool) -> str:
    line = f"poll {'FAILED' if record.get('failed') else 'ok'}; failures {record.get('failures')}"
    if record.get("dry_run"):
        line += " (dry run: actions assumed to succeed)"
    return line


_SKIP = {"ts", "level", "event", "pid", "watch", "poll_id"}


def _generic(record: dict[str, Any]) -> str:
    rest = {key: value for key, value in record.items() if key not in _SKIP}
    return f"{record.get('event')} {json.dumps(rest, ensure_ascii=False, default=str)}"


_FORMATTERS: dict[str, Callable[[dict[str, Any], bool], str]] = {
    "poll.start": _poll_start,
    "hook.end": _hook_end,
    "check.outcome": _check_outcome,
    "command": _command,
    "plugin.log": _plugin_log,
    "poll.end": _poll_end,
}


def format_record(record: dict[str, Any], *, verbose: bool = False) -> str:
    """One human-readable line (plus indented blocks) for a log record."""
    time_part = str(record.get("ts", ""))[11:19]
    prefix = f"{time_part} {record.get('watch', '')}".strip()
    formatter = _FORMATTERS.get(str(record.get("event")))
    body = formatter(record, verbose) if formatter else _generic(record)
    return f"{prefix} {body}" if prefix else body


class ConsolePrinter:
    """A log sink that prints records to a Console."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose

    def __call__(self, record: dict[str, Any]) -> None:
        style = LEVEL_STYLES.get(str(record.get("level", "INFO")), "")
        self.console.print(Text(format_record(record, verbose=self.verbose), style=style))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_logstore.py tests/test_output.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/logstore.py src/keepwatch/output.py tests/test_logstore.py tests/test_output.py
git commit -m "Add JSONL log writer with rotation and human-readable record formatting"
```

---

### Task 11: The poll engine

**Files:**
- Create: `src/keepwatch/pollengine.py`
- Test: `tests/test_pollengine.py`

**Interfaces:**
- Consumes: `WatchConfig`, `GlobalConfig` (Tasks 3–4); `CHECK`, `NO_CHECK`, `resolve_hooks` (Task 3); `Outcome`, `WatchState`, `plan_poll`, `finish_poll` (Task 5); `HookCall`, `HookResult`, `Runner` (Tasks 8–9); `make_record`, `Sink` (Task 10); `Paths` (Task 2).
- Produces:
  - `Fake(outcome: Outcome, payload: Any = None)` (frozen); `parse_fakes(spec: str) -> list[Outcome]` (raises `ValueError` naming the valid outcomes)
  - `PollReport` dataclass: `poll_id, watch, outcome: Outcome, reason, payload, before: WatchState, after: WatchState, planned: tuple[str, ...], results: list[HookResult], failed: bool, faked: bool, dry_run: bool`, with `to_dict() -> dict`
  - `PollEngine(*, runner: Runner, paths: Paths, global_config: GlobalConfig, sink: Sink, pid: int)` with `poll(watch: WatchConfig, state: WatchState, *, fake: Fake | None = None, dry_run: bool = False) -> PollReport`
  - Records emitted per poll, all carrying `watch` and `poll_id`: `poll.start` (`condition, faked, dry_run`), for each hook run zero or more `command`/`plugin.log` then `hook.end` (`HookResult.to_record()`), `check.outcome` (`outcome, reason, payload, condition_before, condition_after, pending_edge, actions, faked`), `poll.end` (`failed, failures, condition, pending_edge, dry_run`)

- [ ] **Step 1: Write the failing tests** — `tests/test_pollengine.py`

```python
import os

import pytest

from keepwatch.config import load_global_config, load_watch_config
from keepwatch.pollengine import Fake, PollEngine, parse_fakes
from keepwatch.runner import Runner
from keepwatch.state import Edge, Outcome, WatchState

PSG_WATCH = '''
    from pathlib import Path

    def check(ctx):
        files = [str(f) for f in ctx.glob(ctx.settings["pattern"])
                 if ctx.file_key(f) not in ctx.ledger("sent")]
        return bool(files), files

    def on_true(ctx):
        sent = ctx.ledger("sent")
        for f in ctx.payload:
            ctx.run(["cp", f, ctx.settings["dest"]])
            sent.add(ctx.file_key(f))
'''


@pytest.fixture
def engine_for(xdg):
    def make(records):
        global_config = load_global_config(xdg.config_file, xdg)
        return PollEngine(runner=Runner(), paths=xdg, global_config=global_config, sink=records.append, pid=os.getpid())

    return make


def test_parse_fakes():
    assert parse_fakes("true, FALSE,timeout") == [Outcome.TRUE, Outcome.FALSE, Outcome.TIMEOUT]
    with pytest.raises(ValueError, match="'maybe' is not an outcome; use a comma-separated list of: true, false"):
        parse_fakes("true,maybe")


def test_psg_style_watch_sends_once(tmp_path, make_watch, engine_for):
    incoming, dest = tmp_path / "incoming", tmp_path / "dest"
    incoming.mkdir()
    dest.mkdir()
    (incoming / "psg-export-1.tar.gz").write_text("one")
    watch_dir = make_watch("psg", config=f'''
        [settings]
        pattern = "{incoming}/psg-export-*.tar.gz"
        dest = "{dest}"
    ''', files={"watch.py": PSG_WATCH})
    watch = load_watch_config(watch_dir)
    records = []
    engine = engine_for(records)

    first = engine.poll(watch, WatchState(False))
    assert first.outcome is Outcome.TRUE
    assert first.planned == ("on_true",)
    assert first.failed is False and first.after == WatchState(True, None, 0)
    assert (dest / "psg-export-1.tar.gz").read_text() == "one"

    second = engine.poll(watch, first.after)
    assert second.outcome is Outcome.FALSE and second.planned == ()

    events = [r["event"] for r in records if r["poll_id"] == first.poll_id]
    assert events == ["poll.start", "hook.end", "check.outcome", "command", "hook.end", "poll.end"]
    assert all(r["watch"] == "psg" for r in records)


def test_failed_edge_is_retried_until_it_succeeds(make_watch, engine_for):
    watch_dir = make_watch("edge", files={"watch.py": '''
        def check(ctx):
            return True

        def on_rise(ctx):
            flag = ctx.watch_dir / "allow"
            if not flag.exists():
                raise RuntimeError("not yet")
    '''})
    watch = load_watch_config(watch_dir)
    engine = engine_for([])
    first = engine.poll(watch, WatchState(False))
    assert first.failed and first.after == WatchState(True, Edge.RISE, 1)
    (watch_dir / "allow").write_text("")
    second = engine.poll(watch, first.after)
    assert second.planned == ("on_rise",)
    assert not second.failed and second.after == WatchState(True, None, 0)


def test_dry_run_does_not_run_actions(make_watch, engine_for):
    watch_dir = make_watch("dry", files={"watch.py": '''
        def check(ctx):
            return True

        def on_true(ctx):
            (ctx.watch_dir / "ran").write_text("")
    '''})
    report = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False), dry_run=True)
    assert report.planned == ("on_true",) and report.results == []
    assert not (watch_dir / "ran").exists()
    assert report.after == WatchState(True, None, 0)


def test_fake_skips_the_check_and_hands_over_the_payload(make_watch, engine_for):
    watch_dir = make_watch("fake", files={"watch.py": '''
        def check(ctx):
            (ctx.watch_dir / "checked").write_text("")
            return False

        def on_true(ctx):
            (ctx.watch_dir / "payload").write_text(repr(ctx.payload))
    '''})
    records = []
    report = engine_for(records).poll(load_watch_config(watch_dir), WatchState(False), fake=Fake(Outcome.TRUE, ["x"]))
    assert report.faked and report.outcome is Outcome.TRUE
    assert not (watch_dir / "checked").exists()
    assert (watch_dir / "payload").read_text() == "['x']"
    assert records[0]["faked"] is True


def test_syntax_error_in_watch_py_is_an_error_outcome(make_watch, engine_for):
    watch_dir = make_watch("broken", files={"watch.py": "def check(ctx):\n    return (\n"})
    report = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False))
    assert report.outcome is Outcome.ERROR and report.failed
    assert report.reason.startswith("watch.py line 2: syntax error")
    assert report.after.failures == 1


def test_missing_check_and_duplicate_hooks(make_watch, engine_for):
    no_check = make_watch("nocheck", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    report = engine_for([]).poll(load_watch_config(no_check), WatchState(False))
    assert report.outcome is Outcome.ERROR and report.reason.startswith("the watch has no check")

    both = make_watch("both", config='[hooks]\ncheck = "true"\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    report = engine_for([]).poll(load_watch_config(both), WatchState(False))
    assert report.outcome is Outcome.ERROR and "defined both" in report.reason


def test_report_to_dict(make_watch, engine_for):
    watch_dir = make_watch("d", config='[hooks]\ncheck = "exit 1"\n')
    data = engine_for([]).poll(load_watch_config(watch_dir), WatchState(False)).to_dict()
    assert data["outcome"] == "false" and data["condition_after"] is False
    assert data["planned_actions"] == [] and data["failed"] is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_pollengine.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.pollengine'`

- [ ] **Step 3: Implement** — `src/keepwatch/pollengine.py`

```python
"""One poll of one watch: check (or fake), state machine, actions, log records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from keepwatch.config import GlobalConfig, WatchConfig
from keepwatch.hooks import CHECK, NO_CHECK, resolve_hooks
from keepwatch.logstore import Sink, make_record
from keepwatch.paths import Paths
from keepwatch.runner import HookCall, HookResult, Runner
from keepwatch.state import Outcome, WatchState, finish_poll, plan_poll

_PLUGIN_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_FAILED = ("error", "failed", "timeout")


def parse_fakes(spec: str) -> list[Outcome]:
    """Parse "true,false,timeout" into outcomes."""
    valid = ", ".join(outcome.value for outcome in Outcome)
    outcomes = []
    for part in spec.split(","):
        word = part.strip()
        try:
            outcomes.append(Outcome(word.lower()))
        except ValueError:
            raise ValueError(f"'{word}' is not an outcome; use a comma-separated list of: {valid}") from None
    return outcomes


@dataclass(frozen=True)
class Fake:
    outcome: Outcome
    payload: Any = None


@dataclass
class PollReport:
    poll_id: str
    watch: str
    outcome: Outcome
    reason: str | None
    payload: Any
    before: WatchState
    after: WatchState
    planned: tuple[str, ...]
    results: list[HookResult] = field(default_factory=list)
    failed: bool = False
    faked: bool = False
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "poll_id": self.poll_id,
            "watch": self.watch,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "payload": self.payload,
            "condition_before": self.before.condition,
            "condition_after": self.after.condition,
            "pending_edge": self.after.pending_edge.value if self.after.pending_edge else None,
            "failures": self.after.failures,
            "planned_actions": list(self.planned),
            "results": [{"hook": result.hook, "status": result.status, "reason": result.reason} for result in self.results],
            "failed": self.failed,
            "faked": self.faked,
            "dry_run": self.dry_run,
        }


def _edge(state: WatchState) -> str | None:
    return state.pending_edge.value if state.pending_edge else None


class PollEngine:
    def __init__(self, *, runner: Runner, paths: Paths, global_config: GlobalConfig, sink: Sink, pid: int) -> None:
        self._runner = runner
        self._paths = paths
        self._global = global_config
        self._sink = sink
        self._pid = pid

    def poll(
        self,
        watch: WatchConfig,
        state: WatchState,
        *,
        fake: Fake | None = None,
        dry_run: bool = False,
    ) -> PollReport:
        poll_id = uuid.uuid4().hex[:12]
        tag = {"watch": watch.name, "poll_id": poll_id}
        faked = fake is not None
        self._sink(make_record("poll.start", **tag, condition=state.condition, faked=faked, dry_run=dry_run))
        hooks, problem = resolve_hooks(watch.watch_dir, watch.hooks)
        if problem is None and fake is None and CHECK not in hooks:
            problem = NO_CHECK
        if problem is not None:
            outcome, reason, payload = Outcome.ERROR, problem, None
        elif fake is not None:
            outcome, reason, payload = fake.outcome, "faked with --fake", fake.payload
        else:
            result = self._run(watch, CHECK, poll_id, state.condition, None, watch.check_timeout)
            outcome, reason, payload = result.outcome(), result.reason, result.payload
        plan = plan_poll(state, outcome, hooks)
        answered = outcome in (Outcome.TRUE, Outcome.FALSE, Outcome.UNKNOWN)
        self._sink(
            make_record(
                "check.outcome",
                level="INFO" if answered else "ERROR",
                **tag,
                outcome=outcome.value,
                reason=reason,
                payload=payload,
                condition_before=state.condition,
                condition_after=plan.state.condition,
                pending_edge=_edge(plan.state),
                actions=list(plan.actions),
                faked=faked,
            )
        )
        results: list[HookResult] = []
        if dry_run:
            after, failed = finish_poll(plan, [(hook, True) for hook in plan.actions])
        else:
            for hook in plan.actions:
                result = self._run(watch, hook, poll_id, plan.state.condition, payload, watch.action_timeout)
                results.append(result)
                if not result.succeeded:
                    break
            after, failed = finish_poll(plan, [(result.hook, result.succeeded) for result in results])
        self._sink(
            make_record(
                "poll.end",
                level="WARNING" if failed else "INFO",
                **tag,
                failed=failed,
                failures=after.failures,
                condition=after.condition,
                pending_edge=_edge(after),
                dry_run=dry_run,
            )
        )
        return PollReport(
            poll_id=poll_id,
            watch=watch.name,
            outcome=outcome,
            reason=reason,
            payload=payload,
            before=state,
            after=after,
            planned=plan.actions,
            results=results,
            failed=failed,
            faked=faked,
            dry_run=dry_run,
        )

    def _run(
        self,
        watch: WatchConfig,
        hook: str,
        poll_id: str,
        condition: bool,
        payload: Any,
        timeout: float,
    ) -> HookResult:
        call = HookCall(
            watch=watch,
            hook=hook,
            poll_id=poll_id,
            condition=condition,
            payload=payload,
            data_dir=self._paths.watch_data_dir(watch.name),
            run_dir=self._paths.run_dir(self._pid, watch.name),
            timeout=timeout,
            capture_bytes=self._global.log.capture_bytes,
            environment=self._global.environment,
        )
        result = self._runner.run(call)
        tag = {"watch": watch.name, "poll_id": poll_id, "hook": hook}
        for message in result.messages:
            if message.get("type") == "command":
                fields = {key: value for key, value in message.items() if key != "type"}
                level = "INFO" if message.get("exit_code") == 0 else "WARNING"
                self._sink(make_record("command", level=level, **tag, **fields))
            else:
                level = message.get("level") if message.get("level") in _PLUGIN_LEVELS else "INFO"
                self._sink(
                    make_record(
                        "plugin.log",
                        level=level,
                        **tag,
                        logger=message.get("logger"),
                        message=message.get("message"),
                        fields=message.get("fields") or {},
                        traceback=message.get("traceback"),
                    )
                )
        level = "ERROR" if result.status in _FAILED else "INFO"
        self._sink(make_record("hook.end", level=level, watch=watch.name, poll_id=poll_id, **result.to_record()))
        return result
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_pollengine.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/pollengine.py tests/test_pollengine.py
git commit -m "Add the poll engine: check, state machine, actions and log records"
```

---

### Task 12: CLI skeleton and `keepwatch poll`

**Files:**
- Create: `src/keepwatch/cli.py`
- Test: `tests/test_cli_poll.py`

**Interfaces:**
- Consumes: Tasks 2–11.
- Produces:
  - Console script `keepwatch` → `keepwatch.cli:main`
  - `cli` (rich-click group named `keepwatch`, options `--config PATH` / env `KEEPWATCH_CONFIG`, `--version`, `-h/--help`), `help_config(plain: bool) -> RichHelpConfiguration`, `App(paths: Paths, config_path: Path)` with `load_global() -> GlobalConfig`
  - `keepwatch poll NAME [--dry-run] [--fake OUTCOMES] [--payload JSON] [--initial-condition BOOL] [-v] [--json]` — exit 0 if no poll failed, 1 otherwise or on load errors, 2 on usage errors. `--json` prints `{"polls": [PollReport.to_dict()...], "records": [...]}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_poll.py`

```python
import json
import textwrap

from click.testing import CliRunner

from keepwatch.cli import cli

WATCH_PY = '''
    def check(ctx):
        return True, ["a"]

    def on_true(ctx):
        (ctx.watch_dir / "ran").write_text(repr(ctx.payload))
'''


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_help_is_plain_when_piped(xdg):
    for args in (["--help"], ["poll", "--help"]):
        result = run(*args)
        assert result.exit_code == 0
        assert not any(ch in result.output for ch in "╭╮╰╯│─")
        assert "\x1b[" not in result.output
    assert "Develop" in run("--help").output


def test_poll_runs_check_and_action(xdg, make_watch):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w")
    assert result.exit_code == 0, result.output
    assert "outcome true; condition FALSE → TRUE; actions: on_true" in result.output
    assert (watch_dir / "ran").read_text() == "['a']"
    events = [json.loads(line)["event"] for line in xdg.log_file.read_text().splitlines()]
    assert events[0] == "poll.start" and events[-1] == "poll.end"
    assert not xdg.process_dir(__import__("os").getpid()).exists()


def test_poll_fake_sequence_as_json(xdg, make_watch):
    watch_dir = make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w", "--fake", "true,false", "--payload", '["x"]', "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [poll["outcome"] for poll in data["polls"]] == ["true", "false"]
    assert data["polls"][0]["planned_actions"] == ["on_true"]
    assert data["polls"][0]["payload"] == ["x"]
    assert any(record["event"] == "check.outcome" for record in data["records"])
    assert not (watch_dir / "ran").exists()


def test_initial_condition_option(xdg, make_watch):
    make_watch("w", files={"watch.py": "def check(ctx):\n    return False\n\ndef on_fall(ctx):\n    pass\n"})
    data = json.loads(run("poll", "w", "--initial-condition", "true", "--json").output)
    assert data["polls"][0]["planned_actions"] == ["on_fall"]


def test_usage_errors_exit_2(xdg, make_watch):
    make_watch("w", files={"watch.py": WATCH_PY})
    result = run("poll", "w", "--payload", "[1]")
    assert result.exit_code == 2 and "--payload only applies together with --fake" in result.output
    result = run("poll", "w", "--fake", "maybe")
    assert result.exit_code == 2 and "'maybe' is not an outcome" in result.output
    result = run("poll", "w", "--fake", "true", "--payload", "{nope")
    assert result.exit_code == 2 and "not valid JSON" in result.output


def test_unknown_watch_and_bad_config_exit_1(xdg, make_watch):
    make_watch("psg-export", config='intervall = "30s"\n')
    result = run("poll", "psg-exprot")
    assert result.exit_code == 1 and "did you mean 'psg-export'" in result.output
    result = run("poll", "psg-export")
    assert result.exit_code == 1 and "config.toml:1: unknown key 'intervall'" in result.output


def test_failed_poll_exits_1(xdg, make_watch):
    make_watch("w", config='[hooks]\ncheck = "exit 9"\n')
    result = run("poll", "w")
    assert result.exit_code == 1
    assert "check → error" in result.output


def test_config_option_selects_watch_dirs(xdg, tmp_path, make_watch):
    other = tmp_path / "elsewhere"
    make_watch("x", files={"watch.py": WATCH_PY}, base=other)
    config = tmp_path / "custom.toml"
    config.write_text(textwrap.dedent(f'watch_dirs = ["{other}"]\n'))
    assert run("--config", str(config), "poll", "x").exit_code == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_poll.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'keepwatch.cli'`

- [ ] **Step 3: Implement** — `src/keepwatch/cli.py`

```python
"""The keepwatch command line (rich-click).

On a terminal, help and output use Rich formatting. When stdout is not a
terminal (how agents run commands) or NO_COLOR is set, output is plain: no
color and no box-drawing characters.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import rich_click as click

from keepwatch import __version__
from keepwatch.config import (
    ConfigError,
    GlobalConfig,
    WatchConfig,
    WatchNotFound,
    discover_watches,
    find_watch,
    load_global_config,
    load_watch_config,
)
from keepwatch.locks import hold_lock
from keepwatch.logstore import LogWriter, fan_out
from keepwatch.output import ConsolePrinter, make_console, plain_output
from keepwatch.paths import PathError, Paths, ensure_private_dir, remove_stale_process_dirs, resolve_paths
from keepwatch.pollengine import Fake, PollEngine, PollReport, parse_fakes
from keepwatch.runner import Runner
from keepwatch.state import initial_state

COMMAND_GROUPS = {"keepwatch": [{"name": "Develop", "commands": ["validate", "poll"]}]}
_PLAIN_BOXES = {
    "style_commands_panel_box": "SIMPLE_HEAD",
    "style_options_panel_box": "SIMPLE_HEAD",
    "style_errors_panel_box": "SIMPLE_HEAD",
}


def help_config(plain: bool) -> click.RichHelpConfiguration:
    if plain:
        return click.RichHelpConfiguration(
            command_groups=COMMAND_GROUPS,
            text_markup=None,
            color_system=None,
            **_PLAIN_BOXES,
        )
    return click.RichHelpConfiguration(command_groups=COMMAND_GROUPS, text_markup="markdown")


@dataclass
class App:
    paths: Paths
    config_path: Path

    def load_global(self) -> GlobalConfig:
        return load_global_config(self.config_path, self.paths)


def _fail(message: str) -> NoReturn:
    make_console(stderr=True).print(message)
    raise SystemExit(1)


def _load_one(app: App, name: str) -> tuple[GlobalConfig, WatchConfig]:
    try:
        global_config = app.load_global()
        watch_dir = find_watch(discover_watches(global_config), name)
        watch = load_watch_config(watch_dir, global_config.defaults)
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    except WatchNotFound as exc:
        _fail(str(exc))
    return global_config, watch


@contextmanager
def _process_dir(paths: Paths) -> Iterator[int]:
    """Prepare the private runtime dir for this process and remove it afterwards."""
    pid = os.getpid()
    try:
        ensure_private_dir(paths.runtime)
        remove_stale_process_dirs(paths)
    except PathError as exc:
        _fail(str(exc))
    try:
        yield pid
    finally:
        shutil.rmtree(paths.process_dir(pid), ignore_errors=True)


@click.group(name="keepwatch", context_settings={"help_option_names": ["-h", "--help"]})
@click.rich_config(help_config=help_config(plain_output()))
@click.version_option(__version__, prog_name="keepwatch")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    envvar="KEEPWATCH_CONFIG",
    help="Global config file. Default: $XDG_CONFIG_HOME/keepwatch/config.toml. Env: KEEPWATCH_CONFIG.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    """Poll conditions and run actions.

    Each watch is a directory holding a config.toml plus the code for its check and actions.
    """
    paths = resolve_paths()
    ctx.obj = App(paths=paths, config_path=config_path or paths.config_file)


@cli.command()
@click.argument("name")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run the real check but only report which actions would run (they are assumed to succeed). "
    "With --fake, nothing runs at all.",
)
@click.option(
    "--fake",
    "fake_spec",
    metavar="OUTCOMES",
    help="Skip the check and feed these outcomes, one poll each, e.g. true,true,false,timeout. "
    "Valid: true, false, unknown, timeout, error. Actions run for real unless --dry-run.",
)
@click.option(
    "--payload",
    "payload_json",
    metavar="JSON",
    help='With --fake: the payload handed to the actions, e.g. \'["a.tar.gz"]\'.',
)
@click.option(
    "--initial-condition",
    type=click.BOOL,
    default=None,
    help="Condition before the first poll (true or false). Default: the watch's initial_condition.",
)
@click.option("-v", "--verbose", is_flag=True, help="Also print captured output and payloads of successful hooks.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON document (polls and all log records) at the end.")
@click.pass_obj
def poll(
    app: App,
    name: str,
    dry_run: bool,
    fake_spec: str | None,
    payload_json: str | None,
    initial_condition: bool | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Run one poll of watch NAME now, printing everything as it happens.

    Every record also goes to the log file. A manual poll shares the watch's persistent data (ledgers)
    with the service but not its condition or failure count, and holds the watch's lock so the service
    and a manual poll never run the same watch at once.

    Exit status: 0 if no poll failed, 1 if a poll failed or the watch could not be loaded, 2 for bad usage.
    """
    fakes = None
    if fake_spec is not None:
        try:
            fakes = parse_fakes(fake_spec)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--fake") from None
    payload = None
    if payload_json is not None:
        if fakes is None:
            raise click.UsageError("--payload only applies together with --fake")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"not valid JSON: {exc}", param_hint="--payload") from None
    global_config, watch = _load_one(app, name)
    records: list[dict] = []
    writer = LogWriter(app.paths.log_file, max_bytes=global_config.log.max_bytes, backups=global_config.log.backups)
    display = records.append if as_json else ConsolePrinter(make_console(), verbose=verbose)
    reports: list[PollReport] = []
    with _process_dir(app.paths) as pid:
        engine = PollEngine(
            runner=Runner(),
            paths=app.paths,
            global_config=global_config,
            sink=fan_out(writer.write, display),
            pid=pid,
        )
        state = initial_state(watch.initial_condition if initial_condition is None else initial_condition)

        def waiting() -> None:
            make_console(stderr=True).print(f"{name} is being polled by another keepwatch process; waiting…")

        with hold_lock(app.paths.watch_lock(watch.name), on_wait=waiting):
            for outcome in fakes or [None]:
                fake = None if outcome is None else Fake(outcome, payload)
                report = engine.poll(watch, state, fake=fake, dry_run=dry_run)
                reports.append(report)
                state = report.after
    if as_json:
        click.echo(json.dumps({"polls": [r.to_dict() for r in reports], "records": records}, indent=2, default=str))
    raise SystemExit(1 if any(report.failed for report in reports) else 0)


def main() -> None:
    cli()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_poll.py -q`
Expected: PASS

- [ ] **Step 5: Try it by hand**

```bash
mkdir -p ~/.config/keepwatch/watches/hello
printf 'interval = "10s"\n' > ~/.config/keepwatch/watches/hello/config.toml
printf 'def check(ctx):\n    return True, ["hi"]\n\ndef on_true(ctx):\n    ctx.log.info("payload %%s", ctx.payload)\n' > ~/.config/keepwatch/watches/hello/watch.py
uv run keepwatch poll hello
uv run keepwatch poll hello --fake true,false,timeout --dry-run
uv run keepwatch --help | cat
rm -r ~/.config/keepwatch/watches/hello
```

Expected: live lines for each poll; the piped `--help` shows no box characters.

- [ ] **Step 6: Commit**

```bash
git add src/keepwatch/cli.py tests/test_cli_poll.py
git commit -m "Add the keepwatch CLI with the poll command"
```

---

### Task 13: `keepwatch validate`

**Files:**
- Create: `src/keepwatch/validation.py`
- Modify: `src/keepwatch/cli.py` (add the `validate` command and its imports)
- Test: `tests/test_cli_validate.py`

**Interfaces:**
- Consumes: Tasks 3, 4, 8, 12.
- Produces:
  - `validation.WatchCheck(name: str, path: Path | None, problems: list[str])` with `ok` property and `to_dict()`
  - `missing_executable(command: Command, watch: WatchConfig, environment: Mapping[str, str]) -> str | None`
  - `check_watch(watch_dir, global_config, runner, paths, pid) -> WatchCheck`
  - `validate_watches(global_config, discovery, names: list[str], runner, paths, pid) -> tuple[list[str], list[WatchCheck]]` — (general problems, per-watch results)
  - `keepwatch validate [NAMES...] [--json]` — exit 0 if everything is valid, 1 otherwise; JSON `{"ok", "problems", "watches": [{"name", "path", "ok", "problems"}]}`

- [ ] **Step 1: Write the failing tests** — `tests/test_cli_validate.py`

```python
import json

from click.testing import CliRunner

from keepwatch.cli import cli


def run(*args):
    return CliRunner().invoke(cli, list(args))


def test_valid_watches(xdg, make_watch):
    make_watch("a", files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("b", config='[hooks]\ncheck = ["true"]\n')
    result = run("validate")
    assert result.exit_code == 0, result.output
    assert "ok   a" in result.output and "ok   b" in result.output


def test_every_problem_is_reported(xdg, make_watch):
    make_watch("typo", config='intervall = "30s"\n', files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("importerr", files={"watch.py": "import does_not_exist_xyz\n\ndef check(ctx):\n    return True\n"})
    make_watch("nocheck", files={"watch.py": "def on_true(ctx):\n    pass\n"})
    make_watch("badcmd", config='[hooks]\ncheck = ["./missing.sh"]\non_true = ["no-such-program-xyz"]\n')
    result = run("validate", "--json")
    assert result.exit_code == 1
    data = json.loads(result.output)
    by_name = {w["name"]: w for w in data["watches"]}
    assert "unknown key 'intervall'" in by_name["typo"]["problems"][0]
    assert "importing watch.py failed: ModuleNotFoundError" in by_name["importerr"]["problems"][0]
    assert by_name["nocheck"]["problems"][0].endswith(
        "the watch has no check: define check(ctx) in watch.py or set check in [hooks] of config.toml; "
        "see: keepwatch docs python"
    )
    badcmd = " ".join(by_name["badcmd"]["problems"])
    assert "./missing.sh does not exist" in badcmd and "'no-such-program-xyz' is not on PATH" in badcmd
    assert data["ok"] is False


def test_names_filter_and_unknown_name(xdg, make_watch):
    make_watch("good", files={"watch.py": "def check(ctx):\n    return True\n"})
    make_watch("bad", config="interval = 0\n")
    assert run("validate", "good").exit_code == 0
    result = run("validate", "goood")
    assert result.exit_code == 1 and "did you mean 'good'" in result.output


def test_missing_watches_directory_is_a_problem(xdg):
    result = run("validate")
    assert result.exit_code == 1
    assert "watches directory does not exist" in result.output
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_cli_validate.py -q`
Expected: FAIL with `No such command 'validate'`

- [ ] **Step 3: Implement** — `src/keepwatch/validation.py`

```python
"""`keepwatch validate`: find every problem in watches before they run."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keepwatch.config import (
    Command,
    ConfigError,
    Discovery,
    GlobalConfig,
    WatchConfig,
    WatchNotFound,
    find_watch,
    load_watch_config,
)
from keepwatch.hooks import CHECK, NO_CHECK, WATCH_PY, resolve_hooks
from keepwatch.paths import Paths
from keepwatch.runner import HookCall, Runner


@dataclass
class WatchCheck:
    name: str
    path: Path | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path) if self.path else None, "ok": self.ok, "problems": self.problems}


def missing_executable(command: Command, watch: WatchConfig, environment: Mapping[str, str]) -> str | None:
    """Why a command hook's program cannot run, or None. Shell strings are checked when they run."""
    if command.argv is None:
        return None
    program = command.argv[0]
    if "/" in program:
        path = Path(program) if os.path.isabs(program) else watch.watch_dir / program
        if not path.is_file():
            return f"{program} does not exist"
        if not os.access(path, os.X_OK):
            return f"{program} is not executable (run: chmod +x {path})"
        return None
    search = {**os.environ, **environment, **watch.environment}.get("PATH", "")
    return None if shutil.which(program, path=search) else f"'{program}' is not on PATH"


def check_watch(watch_dir: Path, global_config: GlobalConfig, runner: Runner, paths: Paths, pid: int) -> WatchCheck:
    report = WatchCheck(watch_dir.name, watch_dir)
    try:
        watch = load_watch_config(watch_dir, global_config.defaults)
    except ConfigError as exc:
        report.problems.extend(str(problem) for problem in exc.problems)
        return report
    hooks, problem = resolve_hooks(watch_dir, watch.hooks)
    source = watch_dir / WATCH_PY
    if problem is not None:
        report.problems.append(f"{source}: {problem}; see: keepwatch docs python")
    elif CHECK not in hooks:
        report.problems.append(f"{watch_dir}: {NO_CHECK}; see: keepwatch docs python")
    for hook, command in watch.hooks.items():
        missing = missing_executable(command, watch, global_config.environment)
        if missing is not None:
            report.problems.append(f"{watch.config_file}: [hooks] {hook}: {missing}; see: keepwatch docs executables")
    if watch.python_dependencies:
        report.problems.append(
            f"{watch.config_file}: python_dependencies is not supported by this version of keepwatch; "
            "see: keepwatch docs dependencies"
        )
    elif source.is_file() and problem is None:
        result = runner.run(
            HookCall(
                watch=watch,
                hook=CHECK,
                poll_id="validate",
                condition=watch.initial_condition,
                payload=None,
                data_dir=paths.watch_data_dir(watch.name),
                run_dir=paths.run_dir(pid, watch.name),
                timeout=watch.check_timeout,
                capture_bytes=global_config.log.capture_bytes,
                environment=global_config.environment,
                mode="describe",
            )
        )
        if result.status != "ok":
            report.problems.append(f"{source}: {result.reason}; see: keepwatch docs python")
    return report


def validate_watches(
    global_config: GlobalConfig,
    discovery: Discovery,
    names: list[str],
    runner: Runner,
    paths: Paths,
    pid: int,
) -> tuple[list[str], list[WatchCheck]]:
    general = [str(problem) for problem in discovery.problems]
    results = []
    for name in names or list(discovery.watches):
        try:
            watch_dir = find_watch(discovery, name)
        except WatchNotFound as exc:
            results.append(WatchCheck(name, None, [str(exc)]))
            continue
        results.append(check_watch(watch_dir, global_config, runner, paths, pid))
    return general, results
```

In `src/keepwatch/cli.py`, add `from keepwatch.validation import validate_watches` to the imports and append this command before `def main()`:

```python
@cli.command()
@click.argument("names", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Print the result as one JSON document.")
@click.pass_obj
def validate(app: App, names: tuple[str, ...], as_json: bool) -> None:
    """Check watches for config and code problems, reporting every problem at once.

    Checks each watch's config.toml (types, unknown keys, with line numbers), that it has a check, that no
    hook is defined twice, that command hooks' programs exist, and that watch.py imports cleanly (in a
    worker process, exactly as a poll loads it). With no NAMES, checks every watch.

    Exit status: 0 if everything is valid, 1 if any problem was found.
    """
    try:
        global_config = app.load_global()
    except ConfigError as exc:
        _fail("\n".join(str(problem) for problem in exc.problems))
    discovery = discover_watches(global_config)
    with _process_dir(app.paths) as pid:
        general, checks = validate_watches(global_config, discovery, list(names), Runner(), app.paths, pid)
    ok = (not general or bool(names)) and all(check.ok for check in checks)
    if as_json:
        document = {"ok": ok, "problems": general, "watches": [check.to_dict() for check in checks]}
        click.echo(json.dumps(document, indent=2))
    else:
        console = make_console()
        for problem in general:
            console.print(f"problem  {problem}")
        for check in checks:
            console.print(f"{'ok' if check.ok else 'FAIL':<4} {check.name}")
            for problem in check.problems:
                console.print(f"     {problem}")
        if not checks and not general:
            console.print("no watches found")
    raise SystemExit(0 if ok else 1)
```

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS (every test from Tasks 1–13)

- [ ] **Step 5: Commit**

```bash
git add src/keepwatch/validation.py src/keepwatch/cli.py tests/test_cli_validate.py
git commit -m "Add keepwatch validate"
```

---

## Roadmap: Plans 2 and 3 (written after Plan 1 lands)

These are separate plans so each can be written against Plan 1's real interfaces. They are listed here so the reviewer can see full spec coverage.

**Plan 2: The service.** Spec sections 7 (state kept across polls), 8, 11, 12.2, and the Run/Inspect/Control commands.
- Scheduler thread per watch using `PollEngine`, `next_delay`, `should_go_offline`; per-watch lock shared with `poll`.
- Offline marker (`offline.json`), `retry_after` trial polls, `alert_command` with `KEEPWATCH_ALERT_*`.
- Master tick: reload the global config and watch configs by mtime/size, keeping the last valid config on broken edits, add/remove/change watches, pick up `enable`/`disable`.
- `status.json` writer; single-instance `service.lock`; stale process dir cleanup at start.
- Commands: `run` (live lines on a terminal, WARNING+ when piped), `status`, `logs` (query across rotated files, `--poll`, `-f`, `--json`), `enable`, `disable`, `rename`.

**Plan 3: Setup, dependencies and documentation.** Spec sections 10, 13 (Setup/Reference), 15, 16.
- `python_dependencies` via `uv run --no-project --offline --with …` with an online fallback, and the `PYTHONPATH` shim to the service's own `keepwatch` package; `validate` builds environments.
- `new` (templates: python, shell, expect), `init` (global config, watches dir, `AGENTS.md`, `CLAUDE.md`), `install`/`uninstall` (systemd user unit, PATH capture, `SSH_AUTH_SOCK` warning).
- `docs` topics generated from `WATCH_KEYS`/`GLOBAL_KEYS`/`LOG_KEYS`, `Ctx` docstrings and the rich-click tree, plus the narrative topics and tested example watches (psg-export, curl with `unknown` codes, Expect); docs-coverage tests; the top-level help line pointing agents at `keepwatch docs agent`.
- README rewrite.
