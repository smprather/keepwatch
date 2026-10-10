"""recipe = "mirror": keep a recursive tree in sync in place over ssh/scp.

The destination is mirror-owned and never scanned (spec 5.2). The source side is the truth — walked locally for
a push, watched over one live ssh connection for a pull — and `ctx.checkpoint("mirror")` is the record of what
has been synced: `{"version": 1, "dirs": [...], "files": {path: [size, mtime]}}`. The check proposes a `Plan`;
the action copies, creates and deletes, and promotes the checkpoint as work lands, so an interrupted poll
repeats nothing. A deletion is never inferred from a partial or a quiet view: a push walk that could not read a
subtree answers Unknown, and a pull deletes only on a `gone` the watcher stated.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CHECKPOINT = "mirror"
TOLERANCE = 2.0  # seconds: FAT and NTFS round mtimes to two


@dataclass(frozen=True)
class Plan:
    """What one check decided: directories to create, files to copy, and paths the source no longer has."""

    mkdir: tuple[str, ...] = ()
    copy: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {"mkdir": list(self.mkdir), "copy": list(self.copy), "deleted": list(self.deleted)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Plan:
        return cls(
            mkdir=tuple(payload.get("mkdir") or ()),
            copy=tuple(payload.get("copy") or ()),
            deleted=tuple(payload.get("deleted") or ()),
        )

    def empty(self) -> bool:
        return not (self.mkdir or self.copy or self.deleted)


def ignored(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _depth(path: str) -> int:
    return path.count("/")


def _changed(live: Sequence[float], state: Sequence[float], tolerance: float) -> bool:
    size, mtime = float(live[0]), float(live[1])
    old_size, old_mtime = float(state[0]), float(state[1])
    return size != old_size or abs(mtime - old_mtime) > tolerance


def plan_push(
    live_files: Mapping[str, Sequence[float]],
    live_dirs: Iterable[str],
    state_files: Mapping[str, Sequence[float]],
    state_dirs: Iterable[str],
    *,
    tolerance: float = TOLERANCE,
) -> Plan:
    """Diff a local walk against the checkpoint: what to copy, what to create, what the source no longer has."""
    dirs = set(live_dirs)
    copy = tuple(
        sorted(
            path
            for path, state in live_files.items()
            if path not in state_files or _changed(state, state_files[path], tolerance)
        )
    )
    mkdir = tuple(sorted(dirs - set(state_dirs), key=_depth))
    live = set(live_files) | dirs
    deleted = tuple(sorted((set(state_files) | set(state_dirs)) - live, key=lambda path: (-_depth(path), path)))
    return Plan(mkdir=mkdir, copy=copy, deleted=deleted)


def plan_pull(
    events: Iterable[Mapping[str, Any]],
    state_files: Mapping[str, Sequence[float]],
    state_dirs: Iterable[str],
    *,
    remote_dir: str,
    tolerance: float = TOLERANCE,
) -> Plan:
    """Diff the watcher's events against the checkpoint. Nothing is inferred from silence."""
    root = remote_dir.rstrip("/")
    copy: set[str] = set()
    mkdir: set[str] = set()
    deleted: set[str] = set()
    known_dirs = set(state_dirs)
    for event in events:
        kind = event.get("event")
        if kind == "files":
            for entry in event.get("entries") or ():
                relative = _relative(root, str(entry[0]))
                if relative is None:
                    continue
                state = state_files.get(relative)
                if state is None or _changed([entry[1], entry[2]], state, tolerance):
                    copy.add(relative)
        elif kind == "dirs":
            for path in event.get("paths") or ():
                relative = _relative(root, str(path))
                if relative is not None and relative not in known_dirs:
                    mkdir.add(relative)
        elif kind == "gone":
            for path in event.get("paths") or ():
                relative = _relative(root, str(path))
                if relative is not None:
                    deleted.add(relative)
    return Plan(mkdir=tuple(sorted(mkdir, key=_depth)), copy=tuple(sorted(copy)), deleted=tuple(sorted(deleted)))


def _relative(root: str, path: str) -> str | None:
    """`path` (absolute, as the watcher reports it) relative to `root`, in POSIX form; None when outside."""
    if path == root or not path.startswith(root + "/"):
        return None
    return path[len(root) + 1 :]


def as_document(files: Mapping[str, Sequence[float]], dirs: Iterable[str]) -> dict[str, Any]:
    """The checkpoint document: the source's listing at the last successful sync."""
    return {
        "version": 1,
        "files": {path: [float(state[0]), float(state[1])] for path, state in sorted(files.items())},
        "dirs": sorted(dirs),
    }


def files_of(document: Mapping[str, Any]) -> dict[str, list[float]]:
    return {str(path): [float(state[0]), float(state[1])] for path, state in (document.get("files") or {}).items()}


def dirs_of(document: Mapping[str, Any]) -> list[str]:
    return [str(path) for path in (document.get("dirs") or [])]


def walk_local(
    root: Path, ignore: Sequence[str], settle: float, *, now: float | None = None
) -> tuple[dict[str, list[float]], set[str], list[str]]:
    """Walk `root` for the push direction: relative POSIX paths, ignored names pruned, symlinks skipped.

    Returns (files {relative: [size, mtime]}, dirs {relative}, problems). A subtree that cannot be read is a
    problem and never an absence: the check answers Unknown when the list is not empty.
    """
    files: dict[str, list[float]] = {}
    dirs: set[str] = set()
    problems: list[str] = []
    wall = time.time() if now is None else now
    if not root.is_dir():
        return files, dirs, [f"{root}: not a directory"]
    for current, dirnames, filenames in os.walk(root, onerror=lambda exc: problems.append(_problem(exc))):
        relative_dir = os.path.relpath(current, root)
        prefix = "" if relative_dir == "." else relative_dir.replace(os.sep, "/") + "/"
        keep = []
        for name in sorted(dirnames):
            if ignored(name, ignore) or os.path.islink(os.path.join(current, name)):
                continue
            dirs.add(prefix + name)
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if ignored(name, ignore):
                continue
            path = os.path.join(current, name)
            try:
                info = os.lstat(path)
            except OSError as exc:
                problems.append(_problem(exc, path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if settle and wall - info.st_mtime < settle:
                continue
            files[prefix + name] = [info.st_size, info.st_mtime]
    return files, dirs, problems


def _problem(exc: OSError, path: str | None = None) -> str:
    return f"{path or exc.filename}: {exc.strerror or exc}"
