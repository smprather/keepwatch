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
import shlex
import stat
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keepwatch.ctx import CommandFailed, Ctx, Unknown
from keepwatch.recipes import file_budget
from keepwatch.remote import ssh_run_argv
from keepwatch.transfer import TransferFailed

CHECKPOINT = "mirror"
TOLERANCE = 2.0  # seconds: FAT and NTFS round mtimes to two
PROMOTE_FILES = 500  # entries landed since the last save ...
PROMOTE_BYTES = 64 * 1024 * 1024  # ... or bytes of file, before the checkpoint is saved
BATCH = 100  # paths per remote command line: Windows' ~32k command-line limit stays far away


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


def batches(items: Sequence[str], size: int = BATCH) -> list[list[str]]:
    """`items` in groups of `size`: one remote command line per group."""
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def should_promote(pending: Sequence[tuple[str, int]]) -> bool:
    """Whether enough has landed since the last save: a whole-document save per file is quadratic on a big sync."""
    return len(pending) >= PROMOTE_FILES or sum(size for _, size in pending) >= PROMOTE_BYTES


def reachable(settings: Mapping[str, Any]) -> tuple[str, int]:
    """The host and port to probe: reachable_host/port, else the remote host and port, else 22.

    `remote` may be `user@host` (or `DOMAIN\\user@host`), and the probe wants the host alone.
    """
    host = str(settings["reachable_host"] or settings["remote"])
    host = host.rpartition("@")[2] or host
    port = settings["reachable_port"] or settings["port"] or 22
    return host, int(port)


def transfer_options(settings: Mapping[str, Any]) -> dict[str, Any]:
    """What every mirror transfer carries: the shared scp options, times preserved, no markers."""
    names = ("protocol", "password_env", "password_mode", "port", "identity", "known_hosts")
    options = {name: settings[name] for name in names if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"]), "preserve_mtime": True}


def remote_root(settings: Mapping[str, Any]) -> str:
    """The remote tree's root as an absolute POSIX path (the watcher joins it with "/" too)."""
    return str(settings["remote_dir"]).rstrip("/") or "/"


def remote_path(settings: Mapping[str, Any], relative: str, *, parent: bool = False) -> str:
    """A relative POSIX path as an absolute one on the remote host; `parent` names its directory."""
    root = remote_root(settings)
    if parent:
        holder = Path(relative).parent.as_posix()
        if holder == ".":
            return root
        relative = holder
    return root + relative if root.endswith("/") else root + "/" + relative


def run_ssh(ctx: Ctx, settings: Mapping[str, Any], command: str) -> None:
    """Run one batched remote command (mkdir, rm, find) with the watch's ssh settings."""
    ctx.run(
        ssh_run_argv(
            str(settings["remote"]),
            command,
            port=settings["port"],
            identity=settings["identity"],
            ssh_options=list(settings["ssh_options"]),
            ssh_command=settings["ssh_command"],
        )
    )


def check(ctx: Ctx) -> tuple[bool, dict[str, list[str]]]:
    """Push: walk the local tree, diff it against the checkpoint, and answer with the plan. No ssh at all.

    The host is probed first: no answer is `Unknown` (no failure count, the tree waits). A walk that could not
    read a subtree is `Unknown` too — never a deletion, because a partial view must not look like a source that
    lost files.
    """
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; the tree waits")
    files, dirs, problems = walk_local(Path(settings["local_dir"]), settings["ignore"], settings["settle"])
    if problems:
        raise Unknown("cannot read " + "; ".join(problems[:3]) + (" (and more)" if len(problems) > 3 else ""))
    document = ctx.checkpoint(CHECKPOINT).value or {}
    plan = plan_push(files, dirs, files_of(document), dirs_of(document), tolerance=settings["mtime_tolerance"])
    return (not plan.empty()), plan.as_dict()


def on_true(ctx: Ctx) -> None:
    """Push the plan: directories, then each changed file, then deletions, promoting the checkpoint as work lands.

    A file that fails is logged and the rest of the plan carries on; the first failure is raised at the end, and
    its entry stays out of the checkpoint, so the next poll tries it again.
    """
    settings = ctx.settings
    plan = Plan.from_payload(ctx.payload or {})
    state = ctx.checkpoint(CHECKPOINT)
    document = state.value or {}
    files = files_of(document)
    dirs = set(dirs_of(document))
    options = transfer_options(settings)
    pending: list[tuple[str, int]] = []
    error: Exception | None = None

    def promote(force: bool = False) -> None:
        if force or should_promote(pending):
            state.save(as_document(files, dirs))
            pending.clear()

    try:
        if plan.mkdir:
            for group in batches(plan.mkdir):
                run_ssh(ctx, settings, "mkdir -p " + " ".join(shlex.quote(remote_path(settings, path)) for path in group))
                dirs.update(group)
            promote()
        for index, relative in enumerate(plan.copy):
            source = Path(settings["local_dir"]) / relative
            try:
                info = source.stat()  # recorded before the copy: a change during it is copied again next poll
            except OSError:
                continue  # gone since the check
            if ctx.remaining <= 0:
                error = error or TransferFailed("no time left for the rest of this queue (raise action_timeout)")
                break
            budget = file_budget(ctx.remaining, len(plan.copy) - index, settings["file_timeout"])
            try:
                ctx.transfer.push(
                    str(source),
                    f"{settings['remote']}:{remote_path(settings, relative, parent=True)}",
                    marker="none",
                    deadline=time.time() + budget,
                    **options,
                )
            except (CommandFailed, TransferFailed, OSError) as exc:
                error = error or exc
                ctx.log.warning("pushing %s failed (%s); the rest of the queue is still tried", relative, exc)
                continue
            files[relative] = [info.st_size, info.st_mtime]
            pending.append((relative, info.st_size))
            promote()
        if plan.deleted:
            lost_dirs = [path for path in plan.deleted if path in dirs]
            lost_files = [path for path in plan.deleted if path not in lost_dirs]
            for relative in plan.deleted:
                files.pop(relative, None)
                dirs.discard(relative)
            if settings["delete"] and (lost_files or lost_dirs):
                for group in batches(lost_files):
                    run_ssh(ctx, settings, "rm -f " + " ".join(shlex.quote(remote_path(settings, path)) for path in group))
                # The sweep takes the directories that just became empty (and any other empty one).
                run_ssh(ctx, settings, f"find {shlex.quote(remote_root(settings))} -type d -empty -delete")
            promote()
    except Exception as exc:
        error = error or exc
    promote(force=True)
    if error is not None:
        raise error
