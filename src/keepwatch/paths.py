"""Where keepwatch keeps its files (XDG base directories)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keepwatch import platform
from keepwatch.platform import pid_alive  # noqa: F401  (re-exported)

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


def ensure_private_dir(path: Path) -> Path:
    """Create path (mode 0700) if needed and verify that only the current user can use it."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise PathError(f"cannot create {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise PathError(f"{path} exists and is not a directory")
    if platform.IS_WINDOWS:
        return path  # the user profile's ACL already keeps these directories private
    if info.st_uid != os.getuid():
        raise PathError(f"{path} is owned by uid {info.st_uid}, not you; refusing to use it")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise PathError(f"{path} is accessible by other users (mode {mode:o}); run: chmod 700 {path}")
    return path


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


def write_json_atomic(path: Path, document: Any) -> None:
    """Write JSON so readers see either the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, default=str)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
