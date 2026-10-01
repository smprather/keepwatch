"""keepwatch enable / disable / rename: control through files the service reads."""

from __future__ import annotations

import os
from pathlib import Path

from keepwatch.config import Discovery, WatchNotFound, find_watch, is_valid_watch_name
from keepwatch.locks import LockBusy, hold_lock
from keepwatch.offline import OfflineMarker, clear_offline, iso_time, read_offline, write_offline
from keepwatch.paths import Paths


class ControlError(Exception):
    """A control command was refused; the message says why and what to do."""


def known_watch(paths: Paths, discovery: Discovery, name: str) -> bool:
    return name in discovery.watches or paths.watch_state_dir(name).is_dir()


def require_known(paths: Paths, discovery: Discovery, name: str) -> None:
    if known_watch(paths, discovery, name):
        return
    try:
        find_watch(discovery, name)
    except WatchNotFound as exc:
        raise ControlError(str(exc)) from None


def enable_watch(paths: Paths, name: str) -> bool:
    """Delete offline.json. True if the watch was offline."""
    return clear_offline(paths, name)


def disable_watch(paths: Paths, name: str, now: float) -> bool:
    """Take the watch offline until `keepwatch enable`. False if a user had already disabled it."""
    existing = read_offline(paths, name)
    if existing is not None and existing.by_user:
        return False
    write_offline(paths, name, OfflineMarker(reason="disabled by user", since=iso_time(now), by_user=True))
    return True


def rename_watch(paths: Paths, discovery: Discovery, old: str, new: str) -> tuple[Path, Path]:
    """Rename a watch directory and its state directory together."""
    try:
        old_dir = find_watch(discovery, old)
    except WatchNotFound as exc:
        raise ControlError(str(exc)) from None
    if not is_valid_watch_name(new):
        raise ControlError(f"'{new}' is not a valid watch name: use letters, digits, '_', '.' and '-'")
    if new in discovery.watches:
        raise ControlError(f"a watch named '{new}' already exists: {discovery.watches[new]}")
    new_dir = old_dir.parent / new
    if new_dir.exists():
        raise ControlError(f"{new_dir} already exists")
    old_state = paths.watch_state_dir(old)
    new_state = paths.watch_state_dir(new)
    if new_state.exists():
        raise ControlError(
            f"{new_state} already exists (state left by an earlier watch named '{new}'); "
            "delete it or choose another name"
        )
    try:
        with hold_lock(paths.watch_lock(old), blocking=False):
            os.rename(old_dir, new_dir)
            if old_state.exists():
                new_state.parent.mkdir(parents=True, exist_ok=True)
                os.rename(old_state, new_state)
    except LockBusy:
        raise ControlError(f"'{old}' is being polled right now; try again in a moment") from None
    return new_dir, new_state
