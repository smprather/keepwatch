"""Where watchglass keeps its files (XDG base directories)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchglass import platform
from watchglass.locks import probe_lock
from watchglass.platform import APP, absolute_path, pid_alive  # noqa: F401  (pid_alive re-exported)

LEGACY_APP = "keepwatch"  # the pre-rename name, read once to move config and state


class PathError(Exception):
    """A directory watchglass needs is unsafe or cannot be created."""


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
        return self.logs_dir / "watchglass.jsonl"

    @property
    def status_file(self) -> Path:
        return self.state_home / "status.json"

    @property
    def service_lock(self) -> Path:
        return self.runtime / "service.lock"

    @property
    def stop_request(self) -> Path:
        return self.runtime / "stop.request"

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


def resolve_paths(env: Mapping[str, str] | None = None, uid: int | None = None) -> Paths:
    """watchglass's directories: XDG variables when set, else the platform defaults.

    A one-time rename migration moves the old keepwatch config and state trees here when the new ones do not
    exist (see `migrate_legacy_dirs`).
    """
    env = os.environ if env is None else env
    config_default, state_default, runtime_default = platform.default_app_dirs(env, uid)
    config_base = absolute_path(env.get("XDG_CONFIG_HOME"))
    state_base = absolute_path(env.get("XDG_STATE_HOME"))
    runtime_base = absolute_path(env.get("XDG_RUNTIME_DIR"))
    paths = Paths(
        config_base / APP if config_base else config_default,
        state_base / APP if state_base else state_default,
        runtime_base / APP if runtime_base else runtime_default,
    )
    migrate_legacy_dirs(paths, env, uid)
    return paths


def _legacy_service_alive(status_file: Path) -> bool:
    """Whether the old keepwatch service says it is running and its pid still exists."""
    try:
        document = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    service = document.get("service") if isinstance(document, dict) else None
    if not isinstance(service, dict) or not service.get("running"):
        return False
    pid = service.get("pid")
    return isinstance(pid, int) and pid_alive(pid)


def _legacy_process_alive(runtime: Path) -> bool:
    """Whether a manual `keepwatch poll` still has a live run directory (it holds no service lock)."""
    if not runtime.is_dir():
        return False
    for entry in runtime.iterdir():
        if entry.name.isdigit() and entry.is_dir() and pid_alive(int(entry.name)):
            return True
    return False


def _rename_legacy_logs(state_home: Path) -> None:
    """Keep pre-rename log history visible to `watchglass logs` after the state tree moves."""
    for old in sorted((state_home / "logs").glob(f"{LEGACY_APP}.jsonl*")):
        new = old.with_name(f"{APP}{old.name[len(LEGACY_APP):]}")
        if new.exists():
            continue
        try:
            old.rename(new)
        except OSError:
            print(f"watchglass: could not rename {old} to {new}", file=sys.stderr)


def migrate_legacy_dirs(
    paths: Paths, env: Mapping[str, str] | None = None, uid: int | None = None
) -> list[Path]:
    """Move the pre-rename keepwatch config and state trees once, if the new ones do not exist.

    Run-only data is never moved: the old service's runtime holds locks and process directories that belong to
    a process that must already be stopped. If that old service is still running, refuse rather than pull its
    config out from under it. Both directories existing means a partial or manual migration: leave both alone.
    """
    env = os.environ if env is None else env
    legacy_config_default, legacy_state_default, legacy_runtime_default = platform.default_app_dirs(
        env, uid, app=LEGACY_APP
    )
    config_base = absolute_path(env.get("XDG_CONFIG_HOME"))
    state_base = absolute_path(env.get("XDG_STATE_HOME"))
    runtime_base = absolute_path(env.get("XDG_RUNTIME_DIR"))
    legacy_config = config_base / LEGACY_APP if config_base else legacy_config_default
    legacy_state = state_base / LEGACY_APP if state_base else legacy_state_default
    legacy_runtime = runtime_base / LEGACY_APP if runtime_base else legacy_runtime_default
    moves = [
        (old, new, label)
        for old, new, label in ((legacy_config, paths.config_home, "config"), (legacy_state, paths.state_home, "state"))
        if old != new and old.is_dir() and not new.exists()
    ]
    if not moves:
        return []
    with probe_lock(legacy_runtime / "service.lock") as running:
        if running or _legacy_service_alive(legacy_state / "status.json") or _legacy_process_alive(legacy_runtime):
            raise PathError(
                "the old keepwatch service is running; stop it before upgrading "
                "(systemctl --user stop keepwatch.service, or: keepwatch stop), then run watchglass again"
            )
    # Windows keeps the runtime inside the state directory. Remove it before the move, or it would be
    # carried into the new runtime: run-only data must never move.
    if (any(old == legacy_state for old, _, _ in moves) and legacy_runtime != legacy_state
            and legacy_runtime.is_relative_to(legacy_state)):
        if legacy_runtime.is_dir() and not legacy_runtime.is_symlink():
            shutil.rmtree(legacy_runtime, ignore_errors=True)
    moved = []
    for old, new, label in moves:
        try:
            new.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(old), str(new))
        except OSError as exc:
            raise PathError(
                f"cannot move the old keepwatch {label} directory {old} to {new}: {exc.strerror or exc}; "
                "move it yourself (stop the old service first), then run watchglass again"
            ) from exc
        if old == legacy_state:
            _rename_legacy_logs(new)
        moved.append(new)
        print(f"watchglass: moved {old} to {new}", file=sys.stderr)
    return moved


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
    """Delete run-only directories left behind by watchglass processes that no longer exist."""
    if not paths.runtime.is_dir():
        return []
    removed = []
    for entry in paths.runtime.iterdir():
        if entry.name.isdigit() and entry.is_dir() and not entry.is_symlink() and not alive(int(entry.name)):
            try:
                shutil.rmtree(entry)
            except OSError:
                pass  # a directory a dying process still holds is swept next time
            removed.append(entry)
    return sorted(removed)


def write_json_atomic(path: Path, document: Any) -> None:
    """Write JSON so readers see either the old file or the complete new one, and it is on disk before the rename.

    The flush and fsync match the ledgers: status.json would self-heal after a power cut, but offline.json is
    authoritative — a marker lost mid-write reads as unreadable, which parks the watch as if a user had
    disabled it.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        platform.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
