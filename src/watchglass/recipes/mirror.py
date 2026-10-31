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
import hashlib
import json
import os
import shlex
import stat
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchglass.ctx import CommandFailed, Ctx, Unknown
from watchglass.recipes import file_budget
from watchglass.recipes.pull import cleanup_parts
from watchglass.remote import ssh_argv, ssh_run_argv, watcher_source
from watchglass.transfer import TransferAuthFailed, TransferFailed, with_known_hosts

CHECKPOINT = "mirror"
HASH_CHUNK = 1024 * 1024
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
    unsettled: Iterable[str] = (),
) -> Plan:
    """Diff a local walk against the checkpoint: what to copy, what to create, what the source no longer has.

    `unsettled` are paths the walk deliberately withheld (a file modified inside `settle`): they exist, so
    they are neither a copy nor a deletion. The caller defers them to a later poll (issue 25).
    """
    held = set(unsettled)
    dirs = set(live_dirs)
    copy = tuple(
        sorted(
            path
            for path, state in live_files.items()
            if path not in held and (path not in state_files or _changed(state, state_files[path], tolerance))
        )
    )
    mkdir = tuple(sorted(dirs - set(state_dirs), key=_depth))
    live = (set(live_files) | dirs | held)
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
    """`path` (absolute, as the watcher reports it) relative to `root`, in POSIX form; None when outside.

    The remote decides this string, and the result names a path under local_dir that is created, written or
    deleted, so a part holding an empty, `.` or `..` component is refused: otherwise `/root/../../etc/x`
    would climb out of the destination. A real watcher never reports one.
    """
    if path == root or not path.startswith(root + "/"):
        return None
    relative = path[len(root) + 1 :]
    if any(component in ("", ".", "..") for component in relative.split("/")):
        return None
    return relative


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
) -> tuple[dict[str, list[float]], set[str], list[str], set[str]]:
    """Walk `root` for the push direction: relative POSIX paths, ignored names pruned, symlinks skipped.

    Returns (files {relative: [size, mtime]}, dirs {relative}, problems, unsettled). A file modified inside
    `settle` is *unsettled*: it exists but is deliberately not reported, so the diff must neither copy nor
    delete it. A subtree that cannot be read is a problem and never an absence: the check answers Unknown
    when the list is not empty.
    """
    files: dict[str, list[float]] = {}
    dirs: set[str] = set()
    problems: list[str] = []
    unsettled: set[str] = set()
    wall = time.time() if now is None else now
    if not root.is_dir():
        return files, dirs, [f"{root}: not a directory"], unsettled
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
                unsettled.add(prefix + name)
                continue
            files[prefix + name] = [info.st_size, info.st_mtime]
    return files, dirs, problems, unsettled


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


def check(ctx: Ctx) -> tuple[bool, dict[str, Any]]:
    """Plan the tree's sync: `push` walks the local side, `pull` plans from the watcher's events."""
    if ctx.settings["direction"] == "pull":
        return check_pull(ctx)
    return check_push(ctx)


def check_push(ctx: Ctx) -> tuple[bool, dict[str, Any]]:
    """Push: walk the local tree, diff it against the checkpoint, and answer with the plan. No ssh at all.

    The host is probed first: no answer is `Unknown` (no failure count, the tree waits). A walk that could not
    read a subtree is `Unknown` too — never a deletion, because a partial view must not look like a source that
    lost files.
    """
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; the tree waits")
    files, dirs, problems, unsettled = walk_local(
        Path(settings["local_dir"]), settings["ignore"], settings["settle"]
    )
    if problems:
        raise Unknown("cannot read " + "; ".join(problems[:3]) + (" (and more)" if len(problems) > 3 else ""))
    document = ctx.checkpoint(CHECKPOINT).value or {}
    plan = plan_push(
        files,
        dirs,
        files_of(document),
        dirs_of(document),
        tolerance=settings["mtime_tolerance"],
        unsettled=unsettled,
    )
    return (not plan.empty()), plan.as_dict()


def check_pull(ctx: Ctx) -> tuple[bool, dict[str, Any]]:
    """Pull: plan from the watcher's deltas. A quiet stream is silence, never a deletion (spec 5.6).

    The payload carries the source mtime of every file to copy (`times`), because scp -p cannot always set it
    (Windows' local set times failure) and the action repairs it with os.utime (spec 5.7).
    """
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; the tree waits")
    document = ctx.checkpoint(CHECKPOINT).value or {}
    remote_dir = str(settings["remote_dir"])
    plan = plan_pull(
        ctx.events,
        files_of(document),
        dirs_of(document),
        remote_dir=remote_dir,
        tolerance=settings["mtime_tolerance"],
    )
    return (not plan.empty()), {**plan.as_dict(), "times": times_from(ctx.events, remote_dir=remote_dir)}


def times_from(events: Iterable[Mapping[str, Any]], *, remote_dir: str) -> dict[str, float]:
    """The source mtime of every file a `files` event offers: what repairs a local mtime scp -p could not set."""
    root = remote_dir.rstrip("/")
    times: dict[str, float] = {}
    for event in events:
        if event.get("event") != "files":
            continue
        for entry in event.get("entries") or ():
            relative = _relative(root, str(entry[0]))
            if relative is not None:
                times[relative] = float(entry[2])
    return times


def on_true(ctx: Ctx) -> None:
    """Do the plan: `push` uploads, `pull` downloads (see both below)."""
    if ctx.settings["direction"] == "pull":
        on_true_pull(ctx)
    else:
        on_true_push(ctx)


def uploading_path(settings: Mapping[str, Any], relative: str) -> str:
    """The remote temp a resumable push uploads to before `os.replace` onto the real name."""
    parent = remote_path(settings, relative, parent=True)
    if not parent.endswith("/"):
        parent += "/"
    name = relative.rsplit("/", 1)[-1]
    token = hashlib.sha256(relative.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{parent}.{name}.watchglass-{token}.part"


def _remote_messages(ctx: Ctx, settings: Mapping[str, Any], options: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Run the remote helper in `options`' mode and return its JSON messages (heartbeats included)."""
    argv = ssh_argv(
        str(settings["remote"]),
        port=settings["port"],
        identity=settings["identity"],
        ssh_options=with_known_hosts(settings["ssh_options"], settings["known_hosts"]),
        ssh_command=settings["ssh_command"],
        remote_python=settings["remote_python"],
    )
    try:
        completed = ctx.run(argv, input=watcher_source(options))
    except CommandFailed as exc:
        if exc.permanent_reason:
            raise  # a rejected credential/host key: let the scheduler park the watch (issue 24)
        raise TransferFailed(
            f"{settings['remote']}: a resumable mirror needs Python 3.6+ on the destination for its helper "
            f"(set remote_python, or turn resume off): {exc}"
        ) from exc
    messages = []
    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and isinstance(message.get("event"), str):
            messages.append(message)
    return messages


def _remote_hash(ctx: Ctx, settings: Mapping[str, Any], path: str) -> tuple[int, str] | None:
    """(size, sha256) of the remote temp, or None when it does not exist."""
    try:
        for message in _remote_messages(ctx, settings, {"mode": "hash", "path": path, "heartbeat": 10.0}):
            event = message["event"]
            if event == "hash":
                return int(message["size"]), str(message["sha256"])
            if event == "missing":
                return None
            if event in ("failed", "refused", "changed"):
                raise TransferFailed(f"hashing {path} on {settings['remote']} failed: {message.get('reason') or event}")
    except (KeyError, TypeError, ValueError) as exc:
        raise TransferFailed(f"the remote helper sent a malformed hash answer for {path}: {exc}") from exc
    raise TransferFailed(f"no hash answer from {settings['remote']} for {path}")


def _remote_finalize(ctx: Ctx, settings: Mapping[str, Any], temp: str, final: str) -> None:
    """Rename the verified temp onto the real name (os.replace: atomically, never a partial final)."""
    for message in _remote_messages(
        ctx, settings, {"mode": "finalize", "path": temp, "target": final}
    ):
        event = message["event"]
        if event == "finalized":
            return
        if event in ("missing", "failed"):
            raise TransferFailed(f"finalizing {final} on {settings['remote']} failed: {message.get('reason') or event}")
    raise TransferFailed(f"no finalize answer from {settings['remote']} for {final}")


def _remote_remove(ctx: Ctx, settings: Mapping[str, Any], path: str) -> None:
    """Best-effort removal of an abandoned temp; a failure only means the next push overwrites it."""
    try:
        for message in _remote_messages(ctx, settings, {"mode": "remove", "path": path}):
            event = message["event"]
            if event == "heartbeat":
                continue
            if event == "failed":
                ctx.log.warning("could not remove %s on %s: %s", path, settings["remote"], message.get("reason"))
            return
    except CommandFailed as exc:
        if exc.permanent_reason:
            raise
        ctx.log.warning("could not remove %s on %s: %s", path, settings["remote"], exc)
    except TransferFailed as exc:
        ctx.log.warning("could not remove %s on %s: %s", path, settings["remote"], exc)


def _hash_source(path: Path, prefix: int) -> tuple[int, str, str]:
    """(size, sha256 of the first `prefix` bytes, sha256 of the whole file), read once.

    The prefix hash proves that a remote partial is a prefix of the current file before `reput` continues it.
    """
    whole = hashlib.sha256()
    head = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            if size < prefix:
                head.update(chunk[: prefix - size])
            whole.update(chunk)
            size += len(chunk)
    return size, head.hexdigest(), whole.hexdigest()


def _resumable_push(
    ctx: Ctx,
    settings: Mapping[str, Any],
    source: Path,
    relative: str,
    options: Mapping[str, Any],
    budget: float,
    before: os.stat_result,
) -> bool:
    """Upload one file through a verified remote temp, resuming only when the partial is a proven prefix.

    Returns True when the temp was verified and renamed onto the real name. False when the source changed while
    it was being copied: the verified temp is kept, so the next poll continues it instead of starting over.
    """
    final = remote_path(settings, relative)
    temp = uploading_path(settings, relative)
    proof = _remote_hash(ctx, settings, temp)
    local_size, prefix_hash, full_hash = _hash_source(source, proof[0] if proof is not None else 0)
    resume = False
    if proof is not None:
        remote_size, remote_hash = proof
        if remote_size <= local_size and remote_hash == prefix_hash:
            if remote_size == local_size and remote_hash == full_hash:
                _remote_finalize(ctx, settings, temp, final)  # a previous attempt got it all
                return True
            resume = True
        else:
            # The temp is not a prefix of this version (rewritten, truncated, or larger): start over.
            _remote_remove(ctx, settings, temp)
    ctx.transfer.upload(
        str(source),
        f"{settings['remote']}:{temp}",
        resume=resume,
        deadline=time.time() + budget,
        **options,
    )
    check = _remote_hash(ctx, settings, temp)
    if check is None:
        raise TransferFailed(f"{relative}: the uploaded temp disappeared before it could be verified; it will be retried")
    if check != (local_size, full_hash):
        # The upload may have read past the pre-upload snapshot (the writer appended while it ran), or the
        # bytes may be wrong. Keep the temp: the next poll hashes it against the then-current local prefix and
        # either continues/finalizes it or discards it. Deleting it here would throw away a valid prefix.
        ctx.log.warning(
            "%s changed while it was being uploaded (%d bytes on the destination); keeping the temp for the "
            "next poll to verify", relative, check[0],
        )
        return False
    try:
        after = source.stat()
    except OSError:
        after = None
    if after is None or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        # The upload is a coherent snapshot of the old version; keep its temp so the next poll can continue it
        # when the writer has appended, instead of re-uploading those bytes.
        ctx.log.warning("%s changed while it was being copied; keeping the verified temp for the next poll", relative)
        return False
    _remote_finalize(ctx, settings, temp, final)
    return True


def on_true_push(ctx: Ctx) -> None:
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
    resumable = bool(settings["resume"])
    landed: list[tuple[str, int]] = []
    error: Exception | None = None

    def promote(force: bool = False) -> None:
        if force or should_promote(landed):
            state.save(as_document(files, dirs))
            landed.clear()

    try:
        if plan.mkdir:
            for group in batches(plan.mkdir):
                run_ssh(ctx, settings, "mkdir -p " + " ".join(shlex.quote(remote_path(settings, path)) for path in group))
                dirs.update(group)
            promote()
        for index, relative in enumerate(plan.copy):
            source = Path(settings["local_dir"]) / relative
            try:
                before = source.stat()
            except OSError:
                if resumable:
                    _remote_remove(ctx, settings, uploading_path(settings, relative))
                continue  # gone since the check
            if ctx.remaining <= 0:
                error = error or TransferFailed("no time left for the rest of this queue (raise action_timeout)")
                break
            budget = file_budget(ctx.remaining, len(plan.copy) - index, settings["file_timeout"])
            try:
                if resumable:
                    finalized = _resumable_push(ctx, settings, source, relative, options, budget, before)
                else:
                    ctx.transfer.push(
                        str(source),
                        f"{settings['remote']}:{remote_path(settings, relative, parent=True)}",
                        marker="none",
                        deadline=time.time() + budget,
                        **options,
                    )
                    finalized = True
            except TransferAuthFailed:
                raise  # permanent: repeating it for the rest of the queue would only burn lockout budget
            except (CommandFailed, TransferFailed, OSError) as exc:
                if isinstance(exc, CommandFailed) and exc.permanent_reason:
                    raise  # a rejected login from the remote helper: park instead of trying the next file
                error = error or exc
                ctx.log.warning("pushing %s failed (%s); the rest of the queue is still tried", relative, exc)
                continue
            if not finalized:
                continue  # the verified temp is kept; the next poll resumes it
            try:
                after = source.stat()
            except OSError:
                ctx.log.warning("%s disappeared while it was being copied; the next poll sees it gone", relative)
                continue
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                # The bytes that landed are not a coherent snapshot of the current file. Recording this
                # version would claim a mixed/older copy is synced; leave it out and copy it again next poll.
                ctx.log.warning("%s changed while it was being copied; the next poll copies it again", relative)
                continue
            files[relative] = [after.st_size, after.st_mtime]
            landed.append((relative, after.st_size))
            promote()
        if plan.deleted:
            lost_dirs = [path for path in plan.deleted if path in dirs]
            lost_files = [path for path in plan.deleted if path not in lost_dirs]
            if resumable and lost_files:
                # An abandoned upload temp for a source that is gone is internal state, never user data.
                temps = [uploading_path(settings, path) for path in lost_files]
                for group in batches(temps):
                    run_ssh(ctx, settings, "rm -f " + " ".join(shlex.quote(path) for path in group))
            if settings["delete"] and (lost_files or lost_dirs):
                for group in batches(lost_files):
                    run_ssh(ctx, settings, "rm -f " + " ".join(shlex.quote(remote_path(settings, path)) for path in group))
                # The sweep takes the directories that just became empty (and any other empty one).
                run_ssh(ctx, settings, f"find {shlex.quote(remote_root(settings))} -type d -empty -delete")
            # Forgetting a path is only safe once the remote is rid of it: a failed `rm` raises above, so the
            # checkpoint still holds these and the next poll plans the deletion again (no event comes back to
            # say the file is gone: it is gone locally, which is what made it a deletion in the first place).
            for relative in plan.deleted:
                files.pop(relative, None)
                dirs.discard(relative)
            promote()
    except Exception as exc:
        error = error or exc
    promote(force=True)
    if error is not None:
        raise error


def on_true_pull(ctx: Ctx) -> None:
    """Pull the plan: create directories, copy each changed file (repairing its mtime), then delete.

    Only a `gone` the watcher stated deletes anything, and only with `delete = true`; a `gone` path leaves the
    checkpoint either way, so a reconnect does not report it again (spec 5.6). A file that fails is logged and
    the rest of the plan carries on; its entry stays out of the checkpoint for the next poll to retry.
    """
    settings = ctx.settings
    payload = ctx.payload if isinstance(ctx.payload, Mapping) else {}
    plan = Plan.from_payload(payload)
    times = {str(path): float(mtime) for path, mtime in (payload.get("times") or {}).items()}
    state = ctx.checkpoint(CHECKPOINT)
    document = state.value or {}
    files = files_of(document)
    dirs = set(dirs_of(document))
    options = transfer_options(settings)
    local = Path(settings["local_dir"])
    pending: list[tuple[str, int]] = []
    error: Exception | None = None
    pulled = 0

    def promote(force: bool = False) -> None:
        if force or should_promote(pending):
            state.save(as_document(files, dirs))
            pending.clear()

    try:
        for relative in plan.mkdir:
            try:
                (local / relative).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                error = error or exc
                ctx.log.warning("cannot create %s (%s)", local / relative, exc)
                continue
            dirs.add(relative)
        if plan.mkdir:
            promote()
        for index, relative in enumerate(plan.copy):
            if ctx.remaining <= 0:
                error = error or TransferFailed("no time left for the rest of this queue (raise action_timeout)")
                break
            budget = file_budget(ctx.remaining, len(plan.copy) - index, settings["file_timeout"])
            try:
                final = ctx.transfer.pull(
                    f"{settings['remote']}:{remote_path(settings, relative)}",
                    str((local / relative).parent),
                    on_conflict="overwrite",
                    deadline=time.time() + budget,
                    **options,
                )
                info = _repair_mtime(ctx, final, times.get(relative), settings["mtime_tolerance"])
            except TransferAuthFailed:
                raise  # permanent: repeating it for the rest of the queue would only burn lockout budget
            except (CommandFailed, TransferFailed, OSError) as exc:
                error = error or exc
                ctx.log.warning("pulling %s failed (%s); the rest of the queue is still tried", relative, exc)
                continue
            files[relative] = [info.st_size, info.st_mtime]
            pending.append((relative, info.st_size))
            pulled += 1
            promote()
        if plan.deleted:
            lost_dirs = [path for path in plan.deleted if path in dirs]
            lost_files = [path for path in plan.deleted if path not in lost_dirs]
            for relative in plan.deleted:
                files.pop(relative, None)
                dirs.discard(relative)
            if settings["delete"]:
                for relative in sorted(lost_files, key=lambda path: (-_depth(path), path)):
                    try:
                        (local / relative).unlink(missing_ok=True)
                    except OSError as exc:
                        error = error or exc
                        ctx.log.warning("cannot delete %s (%s)", local / relative, exc)
                for relative in sorted(lost_dirs, key=lambda path: (-_depth(path), path)):
                    try:
                        (local / relative).rmdir()  # empty now, or it holds something that is not ours
                    except OSError:
                        pass
            promote()
        if pulled:
            cleanup_parts(ctx, local, settings["part_max_age"])
    except Exception as exc:
        error = error or exc
    promote(force=True)
    if error is not None:
        raise error


def _repair_mtime(ctx: Ctx, path: Path, wanted: float | None, tolerance: float) -> os.stat_result:
    """Set the mtime scp -p could not (Windows' local set times failure): the watcher's mtime is the truth."""
    info = path.stat()
    if wanted is None or abs(info.st_mtime - wanted) <= tolerance:
        return info
    try:
        os.utime(path, (wanted, wanted))
    except OSError as exc:
        ctx.log.warning("cannot set the times of %s (%s); it carries the transfer time", path, exc)
        return info
    return path.stat()
