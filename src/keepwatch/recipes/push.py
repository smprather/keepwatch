"""recipe = "push": upload settled files from a local folder when the destination is reachable.

check() probes the destination first: no answer means unknown (no failure, no backoff; files wait). Then
it answers TRUE with the settled files (rescanned here; the files observer only wakes the watch). on_true()
pushes each one with its .sha256 marker, then deletes, archives or keeps it (a ledger remembers kept files).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from keepwatch.ctx import CommandFailed, Ctx, Unknown
from keepwatch.recipes import file_budget
from keepwatch.transfer import TransferFailed, parse_endpoint

LEDGER = "pushed"


def transfer_options(settings: Mapping[str, Any]) -> dict[str, Any]:
    names = ("protocol", "password_env", "password_mode", "port", "identity", "known_hosts")
    options = {name: settings[name] for name in names if settings[name] is not None}
    return {**options, "ssh_options": list(settings["ssh_options"])}


def reachable(settings: Mapping[str, Any]) -> tuple[str, int]:
    """The host and port to probe: reachable_host/port, else dest's host and port (else `port`, else 22)."""
    dest = parse_endpoint(settings["dest"])
    host = settings["reachable_host"] or dest.host
    port = settings["reachable_port"] or settings["port"] or dest.port or 22
    return str(host), int(port)


def settled_files(settings: Mapping[str, Any]) -> list[Path]:
    """Regular files in local_dir matching pattern, not ignore, last modified at least `settle` ago."""
    directory = Path(settings["local_dir"])
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    now = time.time()
    found = []
    for path in entries:
        name = path.name
        if not fnmatch.fnmatch(name, settings["pattern"]):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in settings["ignore"]):
            continue
        try:
            if path.is_file() and now - path.stat().st_mtime >= settings["settle"]:
                found.append(path)
        except OSError:
            continue
    return found


def check(ctx: Ctx) -> tuple[bool, list[str]]:
    settings = ctx.settings
    host, port = reachable(settings)
    if not ctx.transfer.tcp_open(host, port, settings["reachable_timeout"]):
        raise Unknown(f"{host}:{port} is not reachable; files wait in {settings['local_dir']}")
    pushed = ctx.ledger(LEDGER) if settings["after"] == "keep" else None
    files = []
    for path in settled_files(settings):
        try:
            if pushed is None or ctx.file_key(path) not in pushed:
                files.append(str(path))
        except OSError:
            continue  # removed since the scan
    return bool(files), files


def _archive(path: Path, directory: Path, keep_for: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    try:
        shutil.move(str(path), str(target))
    except OSError as exc:
        raise TransferFailed(f"cannot archive {path} to {target}: {exc}") from exc
    os.utime(target)  # the archive's own clock starts now
    cutoff = time.time() - keep_for
    for old in directory.iterdir():
        try:
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def on_true(ctx: Ctx) -> None:
    settings = ctx.settings
    options = transfer_options(settings)
    pushed = ctx.ledger(LEDGER)
    files = [Path(name) for name in ctx.payload]
    error: Exception | None = None
    try:
        for index, path in enumerate(files):
            if not path.is_file():
                continue  # moved or deleted since the check
            key = ctx.file_key(path)
            if settings["after"] == "keep" and key in pushed:
                continue
            if ctx.remaining <= 0:
                error = error or TransferFailed("no time left for the rest of this queue (raise action_timeout)")
                break
            # A fair share of what is left, so one slow upload cannot starve the rest of the queue
            # (`file_timeout` caps it harder). A file that fails or runs out of its share is logged, and the
            # queue carries on; the failure is raised at the end so the poll still reports it.
            budget = file_budget(ctx.remaining, len(files) - index, settings["file_timeout"])
            try:
                remote = ctx.transfer.push(path, settings["dest"], marker=settings["marker"],
                                           resume=settings["resume"], deadline=time.time() + budget, **options)
            except (TransferFailed, CommandFailed, OSError) as exc:
                error = error or exc
                ctx.log.warning("pushing %s failed (%s); the rest of the queue is still tried", path.name, exc)
                continue
            ctx.log.info("pushed %s", path.name, extra={"remote": remote})
            if settings["after"] == "delete":
                path.unlink()
            elif settings["after"] == "archive":
                _archive(path, Path(settings["archive_dir"]), settings["keep_for"])
            else:
                pushed.add(key)
    except Exception as exc:
        error = error or exc
    if error is not None:
        raise error
